"""The MQTT topic layout of ``06-mqtt.md`` MQTT-11, built once from the settings.

Every topic and discovery device identifier the service uses is spelt here and nowhere
else; the publisher, the discovery builder and the main loop all ask :class:`Topics`. The
module imports nothing from paho, so the pure discovery builder can use it.
"""

from dyness_b3_bms_mqtt.config import MAX_MODULES, Settings


class Topics:
    """Topics and device identifiers for one configuration.

    ``<B>`` is ``MQTT_BASE_TOPIC``, ``<P>`` is ``HA_DISCOVERY_PREFIX`` and ``<N>`` is
    ``HA_NODE_ID``. Module numbers are decimal without padding; any module in 1..4 is
    accepted, because surplus modules up to four still get a discovery removal (HA-20).
    """

    def __init__(self, settings: Settings) -> None:
        """Take the base topic, discovery prefix and node id from ``settings``."""
        self._base = settings.mqtt_base_topic
        self._prefix = settings.ha_discovery_prefix
        self._node = settings.ha_node_id

    @property
    def status(self) -> str:
        """``<B>/status``: service availability and the last will."""
        return f"{self._base}/status"

    @property
    def stack_state(self) -> str:
        """``<B>/stack/state``: the stack JSON (MQTT-17)."""
        return f"{self._base}/stack/state"

    @property
    def service_state(self) -> str:
        """``<B>/service/state``: the service JSON (MQTT-18)."""
        return f"{self._base}/service/state"

    @property
    def ha_status(self) -> str:
        """``<P>/status``: Home Assistant's birth and will messages."""
        return f"{self._prefix}/status"

    @property
    def stack_id(self) -> str:
        """``<N>_stack``: the stack device's identifier."""
        return f"{self._node}_stack"

    @property
    def discovery_stack(self) -> str:
        """``<P>/device/<N>_stack/config``: the stack device's discovery config."""
        return self._config(self.stack_id)

    def module_state(self, module: int) -> str:
        """``<B>/module/<m>/state``: module ``module``'s JSON (MQTT-16)."""
        return f"{self._base}/module/{_checked(module)}/state"

    def module_availability(self, module: int) -> str:
        """``<B>/module/<m>/availability``: module ``module``'s availability (MQTT-12)."""
        return f"{self._base}/module/{_checked(module)}/availability"

    def module_id(self, module: int) -> str:
        """``<N>_m<m>``: module ``module``'s device identifier."""
        return f"{self._node}_m{_checked(module)}"

    def discovery_module(self, module: int) -> str:
        """``<P>/device/<N>_m<m>/config``: module ``module``'s discovery config."""
        return self._config(self.module_id(module))

    def _config(self, identifier: str) -> str:
        """Return ``<P>/device/<identifier>/config``."""
        return f"{self._prefix}/device/{identifier}/config"


def _checked(module: int) -> int:
    """Return ``module`` if it is an ``int`` in 1..``MAX_MODULES``.

    Raises:
        ValueError: ``module`` is not an integer module number in range.

    """
    if type(module) is not int or not 1 <= module <= MAX_MODULES:
        raise ValueError(f"module {module!r} is not in 1..{MAX_MODULES}")
    return module
