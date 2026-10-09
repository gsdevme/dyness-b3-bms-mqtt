"""The paho-based publisher, the topic layout and the fake publisher (``06-mqtt.md``).

``PahoPublisher`` is driven against an autospec of ``paho.mqtt.client.Client`` returned by
an injected factory, so every call it makes is recorded in order and no network is used.
The paho callbacks are invoked directly, as paho's network thread would.
"""

import ast
import inspect
import logging
import threading
from collections.abc import Iterator
from typing import Any
from unittest import mock

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from dyness_b3_bms_mqtt import discovery, mqtt, topics
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.mqtt import (
    MAX_QUEUED,
    MqttEvent,
    PahoPublisher,
    Publisher,
    Topics,
    is_ha_online,
)
from tests.fakes.publisher import FakePublisher, Published
from tests.support.settings import make_settings

PASSWORD = "pa55-w0rd-secret"


class Harness:
    """A ``PahoPublisher`` on an autospec client, with the factory's call recorded."""

    def __init__(self, settings: Settings) -> None:
        self.client: Any = mock.create_autospec(paho.Client, instance=True)
        self.client.publish.return_value = mock.create_autospec(paho.MQTTMessageInfo, instance=True)
        self.client.publish.return_value.rc = paho.MQTT_ERR_SUCCESS
        self.factory = mock.Mock(return_value=self.client)
        self.publisher = PahoPublisher(settings, client_factory=self.factory)

    def calls(self) -> list[str]:
        """Return the names of the client methods called so far, in order."""
        return [name for name, _, _ in self.client.mock_calls if "." not in name and name]

    def connect(self, reason: str = "Success") -> None:
        """Run ``on_connect`` as paho's thread would after a CONNACK."""
        self.client.on_connect(
            self.client,
            None,
            paho.ConnectFlags(session_present=False),
            ReasonCode(PacketTypes.CONNACK, reason),
            None,
        )

    def disconnect(self, reason: str = "Unspecified error") -> None:
        """Run ``on_disconnect`` as paho's thread would after losing the connection."""
        self.client.on_disconnect(
            self.client,
            None,
            paho.DisconnectFlags(is_disconnect_packet_from_server=False),
            ReasonCode(PacketTypes.DISCONNECT, reason),
            None,
        )

    def message(self, topic: str, payload: bytes) -> None:
        """Run ``on_message`` for a message on ``topic``."""
        message = paho.MQTTMessage(topic=topic.encode())
        message.payload = payload
        self.client.on_message(self.client, None, message)


@pytest.fixture
def harness() -> Harness:
    return Harness(make_settings())


def started(settings: Settings | None = None) -> Harness:
    h = Harness(settings or make_settings())
    h.publisher.start()
    return h


@pytest.mark.req("MQTT-01", "MQTT-02")
def test_client_construction_v2_mqtt311_clean_session() -> None:
    h = Harness(make_settings(MQTT_CLIENT_ID="bms-test_1"))
    h.factory.assert_called_once_with(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="bms-test_1",
        clean_session=True,
        protocol=MQTTProtocolVersion.MQTTv311,
    )
    default = Harness(make_settings())
    assert default.factory.call_args.kwargs["client_id"] == "dyness-b3-bms-mqtt-testhost"
    assert PahoPublisher.__init__.__kwdefaults__ == {"client_factory": paho.Client}
    publishers: list[Publisher] = [h.publisher, FakePublisher()]
    assert len(publishers) == 2


@pytest.mark.req("MQTT-02", "MQTT-06")
def test_credentials_and_no_password_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    anonymous = started()
    anonymous.client.username_pw_set.assert_not_called()

    no_password = started(make_settings(MQTT_USERNAME="ha"))
    no_password.client.username_pw_set.assert_called_once_with("ha", None)

    h = started(make_settings(MQTT_USERNAME="ha", MQTT_PASSWORD=PASSWORD))
    h.client.username_pw_set.assert_called_once_with("ha", PASSWORD)
    h.connect()
    h.connect("Bad user name or password")
    h.disconnect()
    h.client.publish.side_effect = RuntimeError("boom")
    h.publisher.take_events()
    h.connect()
    h.publisher.take_events()
    h.publisher.publish("x", "y", qos=0, retain=False)
    h.publisher.stop()
    assert caplog.records
    assert PASSWORD not in caplog.text
    assert all(PASSWORD not in str(record.args) for record in caplog.records)


@pytest.mark.req("MQTT-03")
def test_tls_with_and_without_ca(tmp_path: Any) -> None:
    plain = started()
    plain.client.tls_set.assert_not_called()

    system = started(make_settings(MQTT_TLS="true"))
    system.client.tls_set.assert_called_once_with(ca_certs=None)

    ca = tmp_path / "ca.pem"
    ca.write_text("not checked by the publisher\n")
    custom = started(make_settings(MQTT_TLS="true", MQTT_CA_FILE=str(ca)))
    custom.client.tls_set.assert_called_once_with(ca_certs=str(ca))
    for h in (plain, system, custom):
        h.client.tls_insecure_set.assert_not_called()
    source = inspect.getsource(mqtt)
    assert "tls_insecure_set" not in source
    assert "cert_reqs" not in source
    assert "CERT_NONE" not in source


@pytest.mark.req("MQTT-04", "MQTT-21")
def test_connect_async_reconnect_delay_loop_start() -> None:
    h = Harness(make_settings(MQTT_HOST="broker.lan", MQTT_PORT="8883", MQTT_KEEPALIVE_S="12.5"))
    assert h.calls() == []
    h.publisher.start()
    h.client.connect_async.assert_called_once_with("broker.lan", 8883, keepalive=13)
    h.client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)
    h.client.max_queued_messages_set.assert_called_once_with(MAX_QUEUED)
    assert MAX_QUEUED == 16
    h.client.loop_start.assert_called_once_with()
    h.client.connect.assert_not_called()
    h.client.loop_forever.assert_not_called()
    calls = h.calls()
    assert calls.index("reconnect_delay_set") < calls.index("connect_async")
    assert calls.index("connect_async") < calls.index("loop_start")
    assert not h.publisher.connected

    default = started()
    default.client.connect_async.assert_called_once_with("localhost", 1883, keepalive=30)


@pytest.mark.req("MQTT-05", "MQTT-06", "MQTT-13", "MQTT-23")
def test_callbacks_only_set_events(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    h = started()
    caplog.clear()
    before = len(h.client.mock_calls)
    seen: list[threading.Thread] = []

    def from_paho_thread() -> None:
        seen.append(threading.current_thread())
        h.connect()
        h.message("homeassistant/status", b"online")
        h.disconnect()
        h.connect()

    worker = threading.Thread(target=from_paho_thread)
    worker.start()
    worker.join()
    assert seen
    assert len(h.client.mock_calls) == before
    assert caplog.records == []
    assert h.publisher.connected

    assert h.publisher.take_events() == {MqttEvent.CONNECTED, MqttEvent.HA_ONLINE}
    assert h.publisher.take_events() == set()
    assert len(h.client.mock_calls) == before
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert messages == [
        ("INFO", "mqtt connected to localhost:1883"),
        ("INFO", "mqtt disconnected from localhost:1883: Unspecified error"),
        ("INFO", "mqtt connected to localhost:1883"),
    ]
    assert all(r.threadName == threading.current_thread().name for r in caplog.records)

    h.disconnect()
    h.connect("Not authorized")
    assert not h.publisher.connected
    assert h.publisher.take_events() == set()
    assert (
        caplog.records[-1].getMessage() == "mqtt connect to localhost:1883 refused: Not authorized"
    )
    assert caplog.records[-1].levelname == "WARNING"

    tree = ast.parse(inspect.getsource(PahoPublisher))
    callbacks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_on_")
    ]
    assert {c.name for c in callbacks} == {"_on_connect", "_on_disconnect", "_on_message"}
    for callback in callbacks:
        called = {
            node.func.attr
            for node in ast.walk(callback)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called <= {"set", "clear", "add", "append", "_notice", "is_set"}, called


@pytest.mark.req("MQTT-06")
def test_failures_never_raise(caplog: pytest.LogCaptureFixture) -> None:
    h = Harness(make_settings(MQTT_TLS="true"))
    for method in (
        "tls_set",
        "will_set",
        "connect_async",
        "loop_start",
        "publish",
        "subscribe",
        "disconnect",
        "loop_stop",
    ):
        getattr(h.client, method).side_effect = OSError(f"{method} failed")
    h.publisher.start()
    h.connect()
    h.publisher.publish("dyness_b3/stack/state", "{}", qos=0, retain=False)
    h.publisher.subscribe("homeassistant/status", 1)
    h.publisher.take_events()
    h.publisher.stop()
    assert "tls_set failed" in caplog.text
    assert "publish failed" in caplog.text
    assert "subscribe failed" in caplog.text
    assert "disconnect failed" in caplog.text
    assert "loop_stop failed" in caplog.text

    rejected = started()
    rejected.connect()
    rejected.client.publish.return_value.rc = paho.MQTT_ERR_QUEUE_SIZE
    rejected.publisher.publish("dyness_b3/stack/state", "{}", qos=0, retain=False)
    rejected.client.publish.return_value.wait_for_publish.side_effect = RuntimeError("no conn")
    rejected.publisher.stop()
    rejected.client.disconnect.assert_called_once_with()
    rejected.client.loop_stop.assert_called_once_with()


@pytest.mark.req("MQTT-08", "MQTT-11")
def test_will_set_before_connect() -> None:
    h = started(make_settings(MQTT_BASE_TOPIC="garage/battery"))
    h.client.will_set.assert_called_once_with(
        "garage/battery/status", "offline", qos=1, retain=True
    )
    calls = h.calls()
    assert calls.index("will_set") < calls.index("connect_async")


@pytest.mark.req("MQTT-10")
def test_stop_offline_bounded_wait() -> None:
    h = started()
    h.connect()
    info = h.client.publish.return_value
    h.publisher.stop()
    h.client.publish.assert_called_once_with("dyness_b3/status", "offline", qos=1, retain=True)
    info.wait_for_publish.assert_called_once_with(2.0)
    calls = h.calls()
    assert calls[-3:] == ["publish", "disconnect", "loop_stop"]
    assert not h.publisher.connected

    unreachable = started()
    unreachable.publisher.stop()
    unreachable.client.publish.assert_not_called()
    assert unreachable.calls()[-2:] == ["disconnect", "loop_stop"]

    never_started = Harness(make_settings())
    never_started.publisher.stop()
    never_started.client.loop_stop.assert_called_once_with()


@pytest.mark.req("MQTT-21", "MQTT-23")
def test_publish_noop_when_disconnected() -> None:
    h = started()
    h.publisher.publish("dyness_b3/stack/state", "{}", qos=0, retain=False)
    h.publisher.subscribe("homeassistant/status", 1)
    h.client.publish.assert_not_called()
    h.client.subscribe.assert_not_called()

    h.connect()
    h.publisher.publish("dyness_b3/stack/state", b"{}", qos=0, retain=False)
    h.publisher.subscribe("homeassistant/status", 1)
    h.client.publish.assert_called_once_with("dyness_b3/stack/state", b"{}", qos=0, retain=False)
    h.client.subscribe.assert_called_once_with("homeassistant/status", qos=1)

    h.disconnect()
    h.publisher.publish("dyness_b3/stack/state", "{}", qos=0, retain=False)
    assert h.client.publish.call_count == 1


@pytest.mark.req("MQTT-13")
def test_ha_online_exact_payload() -> None:
    topic = "homeassistant/status"
    assert is_ha_online(topic, b"online", topic)
    assert is_ha_online(topic, "online", topic)
    for payload in (b"offline", b"Online", b"online\n", b" online", b"", "ONLINE", b'"online"'):
        assert not is_ha_online(topic, payload, topic), payload
    assert not is_ha_online("ha/status", b"online", topic)
    assert not is_ha_online("homeassistant/status/x", b"online", topic)

    h = started(make_settings(HA_DISCOVERY_PREFIX="ha"))
    h.message("ha/status", b"offline")
    h.message("homeassistant/status", b"online")
    h.message("ha/status", b"online ")
    assert h.publisher.take_events() == set()
    h.message("ha/status", b"online")
    assert h.publisher.take_events() == {MqttEvent.HA_ONLINE}


@pytest.mark.req("MQTT-11")
def test_topics() -> None:
    t = Topics(make_settings())
    assert t.status == "dyness_b3/status"
    assert t.module_state(1) == "dyness_b3/module/1/state"
    assert t.module_availability(3) == "dyness_b3/module/3/availability"
    assert t.stack_state == "dyness_b3/stack/state"
    assert t.service_state == "dyness_b3/service/state"
    assert t.ha_status == "homeassistant/status"
    assert t.discovery_stack == "homeassistant/device/dyness_b3_stack/config"
    assert t.discovery_module(2) == "homeassistant/device/dyness_b3_m2/config"
    assert t.stack_id == "dyness_b3_stack"
    assert t.module_id(4) == "dyness_b3_m4"

    named = Topics(
        make_settings(MQTT_BASE_TOPIC="garage/battery", HA_DISCOVERY_PREFIX="ha", HA_NODE_ID="bank")
    )
    assert named.status == "garage/battery/status"
    assert named.module_state(4) == "garage/battery/module/4/state"
    assert named.module_availability(1) == "garage/battery/module/1/availability"
    assert named.ha_status == "ha/status"
    assert named.discovery_stack == "ha/device/bank_stack/config"
    assert named.discovery_module(4) == "ha/device/bank_m4/config"
    for bad in (0, 5, True):
        with pytest.raises(ValueError, match="module"):
            named.module_state(bad)

    assert mqtt.Topics is topics.Topics
    for module in (topics, discovery):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in (
                node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
            )
        }
        assert "paho" not in imported, module.__name__
    assert "/status" not in inspect.getsource(discovery)
    assert "/config" not in inspect.getsource(discovery)


@pytest.mark.req("TEST-18", "MQTT-23")
def test_fake_publisher_records() -> None:
    fake = FakePublisher(make_settings(MQTT_BASE_TOPIC="bat"))
    assert not fake.started and not fake.connected
    fake.start()
    assert fake.started

    fake.publish("bat/stack/state", '{"a":1}', qos=0, retain=False)
    fake.subscribe("homeassistant/status", 1)
    assert fake.published == []
    assert fake.dropped == [Published("bat/stack/state", '{"a":1}', 0, False)]
    assert fake.subscriptions == []
    assert fake.take_events() == set()

    fake.set_connected(True)
    assert fake.connected
    assert fake.take_events() == {MqttEvent.CONNECTED}
    fake.set_connected(False)
    fake.set_connected(True, fire=False)
    assert fake.take_events() == set()

    fake.publish("bat/status", "online", qos=1, retain=True)
    fake.publish("bat/stack/state", b'{"a":2}', qos=0, retain=False)
    fake.publish("bat/stack/state", '{"a":3}', qos=0, retain=False)
    assert fake.published == [
        Published("bat/status", "online", 1, True),
        Published("bat/stack/state", b'{"a":2}', 0, False),
        Published("bat/stack/state", '{"a":3}', 0, False),
    ]
    assert fake.last_json("bat/stack/state") == {"a": 3}
    with pytest.raises(AssertionError, match="nothing published"):
        fake.last_json("bat/service/state")

    fake.deliver("homeassistant/status", "online")
    assert fake.take_events() == set()
    fake.subscribe("homeassistant/status", 1)
    assert fake.subscriptions == [("homeassistant/status", 1)]
    fake.deliver("homeassistant/status", "offline")
    assert fake.take_events() == set()
    fake.deliver("homeassistant/status", b"online")
    assert fake.take_events() == {MqttEvent.HA_ONLINE}

    fake.set_connected(False)
    fake.set_connected(True)
    fake.deliver("homeassistant/status", "online")
    assert fake.take_events() == {MqttEvent.CONNECTED}

    fake.explode_on_take_events = RuntimeError("broker gone")
    with pytest.raises(RuntimeError, match="broker gone"):
        fake.take_events()
    fake.explode_on_take_events = None

    fake.stop()
    assert fake.stopped and not fake.connected
    assert fake.published[-1] == Published("bat/status", "offline", 1, True)

    quiet = FakePublisher()
    quiet.stop()
    assert quiet.stopped
    assert quiet.published == []


def iter_events() -> Iterator[MqttEvent]:
    yield from MqttEvent


@pytest.mark.req("MQTT-23")
def test_event_values() -> None:
    assert {(e.name, e.value) for e in iter_events()} == {
        ("CONNECTED", "connected"),
        ("HA_ONLINE", "ha_online"),
    }
