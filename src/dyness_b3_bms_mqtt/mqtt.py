"""The MQTT publisher interface and its paho-mqtt 2.x implementation (``06-mqtt.md``).

Application code talks to the broker only through :class:`Publisher` (MQTT-23).
:class:`PahoPublisher` implements it over ``paho.mqtt.client.Client``: MQTT 3.1.1, a clean
session, the retained ``offline`` will on ``<B>/status``, a non-blocking ``connect_async``
and paho's own network thread with exponential reconnect back-off (MQTT-01..04, MQTT-08).

Threading: paho runs the callbacks on its network thread, where they only set or clear the
``connected`` event, add to the pending events and append to the pending log notices, all
under one lock (MQTT-05). The main loop calls :meth:`PahoPublisher.take_events` every
iteration, which emits those notices through ``logging`` on the main thread and returns the
events. Every publish and subscribe happens on the main thread.

No broker error ever propagates: every paho call is guarded and a failure is logged
(MQTT-06). ``MQTT_PASSWORD`` is passed to paho and never logged (MQTT-02).
"""

import enum
import logging
import math
import threading
from collections.abc import Callable
from typing import Any, Final, Protocol

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.topics import Topics

__all__ = [
    "MAX_QUEUED",
    "OFFLINE",
    "ONLINE",
    "ClientFactory",
    "MqttEvent",
    "PahoPublisher",
    "Publisher",
    "Topics",
    "is_ha_online",
]

_log = logging.getLogger(__name__)

ONLINE: Final = "online"
OFFLINE: Final = "offline"
STATUS_QOS: Final = 1
MAX_QUEUED: Final = 16
"""Bound on paho's internal queue; state is never buffered while disconnected (MQTT-21)."""
RECONNECT_MIN_DELAY_S: Final = 1
RECONNECT_MAX_DELAY_S: Final = 60
STOP_ACK_TIMEOUT_S: Final = 2.0
"""How long :meth:`PahoPublisher.stop` waits for the ``offline`` publish (MQTT-10)."""

type ClientFactory = Callable[..., paho.Client]
"""Builds the paho client; called with the keyword arguments of ``paho.Client``."""


class MqttEvent(enum.Enum):
    """What happened on paho's thread since the main loop last looked (MQTT-23)."""

    CONNECTED = "connected"
    """A connect or reconnect completed."""
    HA_ONLINE = "ha_online"
    """``<P>/status`` carried exactly ``online``."""


class Publisher(Protocol):
    """The only interface application code uses to talk to MQTT (MQTT-23)."""

    def start(self) -> None:
        """Set the will, connect asynchronously and start the network loop."""

    def stop(self) -> None:
        """Publish ``offline``, wait briefly for it, disconnect and stop (MQTT-10)."""

    @property
    def connected(self) -> bool:
        """Whether the broker connection is currently up."""

    def take_events(self) -> set[MqttEvent]:
        """Return and clear the pending events."""

    def subscribe(self, topic: str, qos: int) -> None:
        """Subscribe to ``topic``; nothing happens while disconnected."""

    def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None:
        """Publish one message; a no-op returning at once while disconnected (MQTT-21)."""


def is_ha_online(topic: str, payload: str | bytes, ha_status_topic: str) -> bool:
    """Return whether a message is Home Assistant's exact ``online`` birth (MQTT-13)."""
    text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
    return topic == ha_status_topic and text == ONLINE


class PahoPublisher:
    """:class:`Publisher` over a paho-mqtt 2.x client.

    The client is built at construction, with no network activity, by ``client_factory``
    (``paho.Client`` unless a test injects another). :meth:`start` configures it and starts
    connecting; nothing here ever raises a broker or paho error to the caller.
    """

    def __init__(self, settings: Settings, *, client_factory: ClientFactory = paho.Client) -> None:
        """Build the client for ``settings`` and attach the callbacks."""
        self._settings = settings
        self._topics = Topics(settings)
        self._where = f"{settings.mqtt_host}:{settings.mqtt_port}"
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._events: set[MqttEvent] = set()
        self._notices: list[tuple[int, str]] = []
        self._client = client_factory(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=True,
            protocol=MQTTProtocolVersion.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        """Whether paho reported a successful connect not yet followed by a disconnect."""
        return self._connected.is_set()

    def start(self) -> None:
        """Configure credentials, TLS, queue bound, back-off and will, then connect (MQTT-01..08).

        ``connect_async`` only records the address and ``loop_start`` starts paho's thread,
        which connects and reconnects in the background, so an unreachable broker neither
        blocks nor fails start-up. A failing step is logged and the remaining steps skipped.
        """
        settings = self._settings
        _log.info(
            "mqtt connecting to %s as %s (tls %s)",
            self._where,
            settings.mqtt_client_id,
            "on" if settings.mqtt_tls else "off",
        )
        client = self._client
        try:
            if settings.mqtt_username is not None:
                client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
            if settings.mqtt_tls:
                client.tls_set(ca_certs=settings.mqtt_ca_file)
            client.max_queued_messages_set(MAX_QUEUED)
            client.reconnect_delay_set(
                min_delay=RECONNECT_MIN_DELAY_S, max_delay=RECONNECT_MAX_DELAY_S
            )
            client.will_set(self._topics.status, OFFLINE, qos=STATUS_QOS, retain=True)
            client.connect_async(
                settings.mqtt_host,
                settings.mqtt_port,
                keepalive=math.ceil(settings.mqtt_keepalive_s),
            )
            client.loop_start()
        except Exception as exc:
            _log.error("mqtt start failed: %s", _describe(exc))

    def stop(self) -> None:
        """Publish ``offline`` if connected, wait up to 2 s for it, disconnect, stop (MQTT-10).

        While disconnected nothing is published and nothing is waited for. Notices still
        pending (such as the final disconnect) are logged and pending events discarded.
        """
        if self.connected:
            try:
                info = self._client.publish(
                    self._topics.status, OFFLINE, qos=STATUS_QOS, retain=True
                )
                info.wait_for_publish(STOP_ACK_TIMEOUT_S)
            except Exception as exc:
                _log.warning("mqtt offline publish failed: %s", _describe(exc))
        self._guarded("disconnect", self._client.disconnect)
        self._guarded("loop_stop", self._client.loop_stop)
        self._connected.clear()
        self.take_events()

    def take_events(self) -> set[MqttEvent]:
        """Log the notices recorded by the callbacks, then return and clear the events."""
        with self._lock:
            events, self._events = self._events, set()
            notices, self._notices = self._notices, []
        for level, message in notices:
            _log.log(level, "%s", message)
        return events

    def subscribe(self, topic: str, qos: int) -> None:
        """Subscribe to ``topic`` while connected; otherwise do nothing."""
        if not self.connected:
            return
        try:
            self._client.subscribe(topic, qos=qos)
        except Exception as exc:
            _log.warning("mqtt subscribe to %s failed: %s", topic, _describe(exc))

    def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None:
        """Hand one message to paho while connected; otherwise return at once (MQTT-21)."""
        if not self.connected:
            return
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            _log.warning("mqtt publish to %s failed: %s", topic, _describe(exc))
            return
        if info.rc != paho.MQTT_ERR_SUCCESS:
            _log.debug("mqtt publish to %s not sent: %s", topic, paho.error_string(info.rc))

    def _guarded(self, name: str, call: Callable[[], object]) -> None:
        """Run one paho call, logging instead of raising if it fails."""
        try:
            call()
        except Exception as exc:
            _log.warning("mqtt %s failed: %s", name, _describe(exc))

    def _notice(self, level: int, message: str) -> None:
        """Queue a log line for :meth:`take_events`; the caller holds the lock."""
        self._notices.append((level, message))

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        """Record a completed connect, or a refused one, on paho's thread."""
        with self._lock:
            if reason_code.is_failure:
                self._notice(
                    logging.WARNING, f"mqtt connect to {self._where} refused: {reason_code}"
                )
                return
            self._connected.set()
            self._events.add(MqttEvent.CONNECTED)
            self._notice(logging.INFO, f"mqtt connected to {self._where}")

    def _on_disconnect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        """Record a lost or closed connection, with its reason, on paho's thread."""
        with self._lock:
            self._connected.clear()
            self._notice(logging.INFO, f"mqtt disconnected from {self._where}: {reason_code}")

    def _on_message(self, client: paho.Client, userdata: Any, message: paho.MQTTMessage) -> None:
        """Record Home Assistant's ``online`` birth message on paho's thread (MQTT-13)."""
        if is_ha_online(message.topic, message.payload, self._topics.ha_status):
            with self._lock:
                self._events.add(MqttEvent.HA_ONLINE)


def _describe(exc: BaseException) -> str:
    """Return ``exc`` as ``Type: message`` for a log line."""
    return f"{type(exc).__name__}: {exc}"
