"""A recording stand-in for the MQTT publisher (TEST-18).

``FakePublisher`` implements the ``Publisher`` protocol of ``06-mqtt.md`` (MQTT-23) without
a broker. Publishes while connected are recorded in order in ``published``; publishes while
disconnected are recorded in ``dropped`` and never delivered (MQTT-21). A test drives the
connection with ``set_connected`` (which fires ``CONNECTED`` on a connect unless told not
to) and delivers incoming messages with ``deliver``. Like a clean MQTT session, a delivery
only reaches the fake when its topic is subscribed on the current connection, and a
reconnect drops every subscription.
"""

import json
from dataclasses import dataclass
from typing import Any

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.mqtt import OFFLINE, MqttEvent, Topics, is_ha_online
from tests.support.settings import make_settings


@dataclass(frozen=True, slots=True)
class Published:
    """One publish as the publisher received it."""

    topic: str
    payload: str | bytes
    qos: int
    retain: bool


class FakePublisher:
    """A ``Publisher`` that records instead of talking to a broker.

    ``settings`` gives the topics (the defaults when omitted). ``explode_on_take_events``,
    when set, is raised by the next and every later ``take_events`` call.
    """

    def __init__(self, settings: Settings | None = None, *, connected: bool = False) -> None:
        """Start disconnected (or connected, without an event), not started and not stopped."""
        self.topics = Topics(settings or make_settings())
        self.published: list[Published] = []
        self.dropped: list[Published] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.started = False
        self.stopped = False
        self.explode_on_take_events: Exception | None = None
        self._connected = connected
        self._active: set[str] = set()
        self._events: set[MqttEvent] = set()

    @property
    def connected(self) -> bool:
        """Whether the fake broker connection is up."""
        return self._connected

    def start(self) -> None:
        """Record that the publisher was started."""
        self.started = True

    def stop(self) -> None:
        """Publish the retained ``offline`` status if connected, then disconnect (MQTT-10)."""
        if self._connected:
            self.publish(self.topics.status, OFFLINE, qos=1, retain=True)
        self.set_connected(False)
        self.stopped = True

    def take_events(self) -> set[MqttEvent]:
        """Return and clear the pending events, or raise ``explode_on_take_events``."""
        if self.explode_on_take_events is not None:
            raise self.explode_on_take_events
        events, self._events = self._events, set()
        return events

    def subscribe(self, topic: str, qos: int) -> None:
        """Record a subscription made while connected; ignore it while disconnected."""
        if self._connected:
            self.subscriptions.append((topic, qos))
            self._active.add(topic)

    def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None:
        """Record the publish in ``published`` when connected and in ``dropped`` otherwise."""
        message = Published(topic, payload, qos, retain)
        (self.published if self._connected else self.dropped).append(message)

    def set_connected(self, value: bool, fire: bool = True) -> None:
        """Bring the connection up or down; a connect fires ``CONNECTED`` when ``fire``."""
        was = self._connected
        self._connected = value
        if not value:
            self._active.clear()
        elif not was and fire:
            self._events.add(MqttEvent.CONNECTED)

    def deliver(self, topic: str, payload: str | bytes) -> None:
        """Deliver an incoming message; ``HA_ONLINE`` fires as ``PahoPublisher`` would."""
        if (
            self._connected
            and topic in self._active
            and is_ha_online(topic, payload, self.topics.ha_status)
        ):
            self._events.add(MqttEvent.HA_ONLINE)

    def last_json(self, topic: str) -> Any:
        """Return the parsed payload of the last message published on ``topic``.

        Raises:
            AssertionError: nothing was published on ``topic``.

        """
        for message in reversed(self.published):
            if message.topic == topic:
                return json.loads(message.payload)
        raise AssertionError(f"nothing published on {topic}")
