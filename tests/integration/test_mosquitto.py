"""``PahoPublisher`` against a real broker (TEST-37, TEST-35).

Every test here is marked ``mosquitto`` and ``integration``: it is skipped unless a broker
accepts a TCP connection at ``MQTT_HOST``:``MQTT_PORT`` and it may wait on real time. Each
test uses its own client ids and base topic so retained messages from other runs cannot
interfere, and an independent paho client observes what the broker delivers.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.discovery import build_discovery
from dyness_b3_bms_mqtt.mqtt import MqttEvent, PahoPublisher, Topics
from tests.plugins.markers import broker_address
from tests.support.settings import make_settings

pytestmark = [pytest.mark.mosquitto, pytest.mark.integration]

WAIT_S = 5.0


@dataclass(frozen=True)
class Received:
    """A message the observer received."""

    topic: str
    payload: bytes
    retain: bool


def wait_for(condition: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    """Poll ``condition`` every 10 ms until it holds or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class Observer:
    """An independent paho client that records every message on its subscriptions."""

    def __init__(self, host: str, port: int, *topics: str) -> None:
        self.messages: list[Received] = []
        self._subscribed = False
        self.client = paho.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"observer-{uuid.uuid4().hex[:12]}",
        )
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe
        self.client.connect(host, port, keepalive=10)
        self.client.subscribe([(topic, 1) for topic in topics])
        self.client.loop_start()
        assert wait_for(lambda: self._subscribed), "observer subscription not acknowledged"

    def _on_message(self, client: paho.Client, userdata: Any, message: paho.MQTTMessage) -> None:
        self.messages.append(Received(message.topic, message.payload, bool(message.retain)))

    def _on_subscribe(self, client: paho.Client, userdata: Any, *args: Any) -> None:
        self._subscribed = True

    def on(self, topic: str) -> list[Received]:
        return [m for m in self.messages if m.topic == topic]

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


@pytest.fixture
def settings() -> Settings:
    """Return settings for the test broker with a base topic and node id unique to the test."""
    host, port = broker_address(os.environ)
    unique = uuid.uuid4().hex[:12]
    return make_settings(
        MQTT_HOST=host,
        MQTT_PORT=str(port),
        MQTT_CLIENT_ID=f"dyness-test-{unique}",
        MQTT_BASE_TOPIC=f"dyness_test/{unique}",
        HA_DISCOVERY_PREFIX=f"ha_test_{unique}",
        HA_NODE_ID=f"node_{unique}",
        MQTT_KEEPALIVE_S="10",
    )


@pytest.fixture
def observe(settings: Settings) -> Iterator[Callable[..., Observer]]:
    """Yield a factory of observers subscribed to the given topics; all closed afterwards."""
    observers: list[Observer] = []

    def make(*topics: str) -> Observer:
        observer = Observer(settings.mqtt_host, settings.mqtt_port, *topics)
        observers.append(observer)
        return observer

    yield make
    for observer in observers:
        observer.close()


class Capture:
    """A client factory that builds real paho clients and keeps them for the test."""

    def __init__(self) -> None:
        self.clients: list[paho.Client] = []

    def __call__(self, **kwargs: Any) -> paho.Client:
        client = paho.Client(**kwargs)
        self.clients.append(client)
        return client


def connected(publisher: PahoPublisher) -> PahoPublisher:
    """Start ``publisher`` and wait until its connect event is taken on this thread."""
    publisher.start()
    assert wait_for(lambda: publisher.connected), "publisher did not connect"
    assert MqttEvent.CONNECTED in publisher.take_events()
    return publisher


def clear_retained(settings: Settings, topics: list[str]) -> None:
    """Remove the retained messages this test left on ``topics``."""
    host, port = settings.mqtt_host, settings.mqtt_port
    cleaner = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    cleaner.connect(host, port)
    cleaner.loop_start()
    infos = [cleaner.publish(topic, b"", qos=1, retain=True) for topic in topics]
    for info in infos:
        info.wait_for_publish(2.0)
    cleaner.disconnect()
    cleaner.loop_stop()


@pytest.mark.req("TEST-37", "MQTT-11", "HA-02")
def test_retained_discovery_and_availability(
    settings: Settings, observe: Callable[..., Observer]
) -> None:
    topics = Topics(settings)
    publisher = connected(PahoPublisher(settings))
    publisher.publish(topics.status, "online", qos=1, retain=True)
    discovery = build_discovery(settings, "0.0.0-test")
    for topic, payload in discovery:
        publisher.publish(topic, payload, qos=1, retain=True)
    for module in range(1, settings.module_count + 1):
        publisher.publish(topics.module_availability(module), "online", qos=1, retain=True)
    publisher.publish(topics.stack_state, '{"ts":"x"}', qos=0, retain=False)
    publisher.stop()

    retained = [topic for topic, payload in discovery if payload]
    expected = len(retained) + settings.module_count + 1
    late = observe(f"{settings.ha_discovery_prefix}/#", f"{settings.mqtt_base_topic}/#")
    try:
        assert wait_for(lambda: len(late.messages) >= expected)
        time_for_strays = wait_for(lambda: len(late.messages) > expected, timeout=0.3)
        assert not time_for_strays, late.messages
        assert all(m.retain for m in late.messages)
        by_topic = {m.topic: m.payload for m in late.messages}
        for topic, payload in discovery:
            if payload:
                assert by_topic[topic] == payload.encode()
            else:
                assert topic not in by_topic
        for module in range(1, settings.module_count + 1):
            assert by_topic[topics.module_availability(module)] == b"online"
        assert by_topic[topics.status] == b"offline"
        assert topics.stack_state not in by_topic
    finally:
        clear_retained(
            settings,
            [*retained, topics.status]
            + [topics.module_availability(m) for m in range(1, settings.module_count + 1)],
        )


@pytest.mark.req("TEST-37", "MQTT-08")
def test_lwt_on_abrupt_close(settings: Settings, observe: Callable[..., Observer]) -> None:
    topics = Topics(settings)
    capture = Capture()
    publisher = connected(PahoPublisher(settings, client_factory=capture))
    watcher = observe(topics.status)
    publisher.publish(topics.status, "online", qos=1, retain=True)
    assert wait_for(lambda: [m.payload for m in watcher.on(topics.status)] == [b"online"])

    (client,) = capture.clients
    client.loop_stop()
    sock = client.socket()
    assert sock is not None
    sock.close()
    try:
        assert wait_for(lambda: b"offline" in [m.payload for m in watcher.on(topics.status)])
        late = observe(topics.status)
        assert wait_for(lambda: bool(late.on(topics.status)))
        assert late.on(topics.status) == [Received(topics.status, b"offline", True)]
    finally:
        clear_retained(settings, [topics.status])


@pytest.mark.req("TEST-37", "MQTT-10")
def test_explicit_offline_on_stop(settings: Settings, observe: Callable[..., Observer]) -> None:
    topics = Topics(settings)
    publisher = connected(PahoPublisher(settings))
    watcher = observe(topics.status)
    publisher.publish(topics.status, "online", qos=1, retain=True)
    assert wait_for(lambda: bool(watcher.on(topics.status)))

    started = time.monotonic()
    publisher.stop()
    assert time.monotonic() - started < 2.5
    assert not publisher.connected
    try:
        assert wait_for(lambda: len(watcher.on(topics.status)) == 2)
        assert [m.payload for m in watcher.on(topics.status)] == [b"online", b"offline"]
        late = observe(topics.status)
        assert wait_for(lambda: bool(late.on(topics.status)))
        assert late.on(topics.status) == [Received(topics.status, b"offline", True)]
    finally:
        clear_retained(settings, [topics.status])


@pytest.mark.req("TEST-37", "MQTT-04", "MQTT-21")
def test_reconnect_after_drop(settings: Settings, observe: Callable[..., Observer]) -> None:
    topics = Topics(settings)
    publisher = connected(PahoPublisher(settings))
    watcher = observe(topics.stack_state)

    intruder = paho.Client(
        callback_api_version=CallbackAPIVersion.VERSION2, client_id=settings.mqtt_client_id
    )
    intruder.connect(settings.mqtt_host, settings.mqtt_port)
    intruder.loop_start()
    try:
        assert wait_for(lambda: not publisher.connected), "broker did not drop the publisher"
        publisher.publish(topics.stack_state, '{"n":1}', qos=0, retain=False)
    finally:
        intruder.disconnect()
        intruder.loop_stop()

    assert wait_for(lambda: publisher.connected), "publisher did not reconnect"
    assert MqttEvent.CONNECTED in publisher.take_events()
    publisher.publish(topics.stack_state, '{"n":2}', qos=0, retain=False)
    assert wait_for(lambda: bool(watcher.on(topics.stack_state)))
    publisher.stop()
    assert [m.payload for m in watcher.on(topics.stack_state)] == [b'{"n":2}']
    clear_retained(settings, [topics.status])
