"""Dyness B3 BMS to MQTT bridge.

Reads a stack of Dyness B3 battery modules over CAN and publishes their telemetry to MQTT
with Home Assistant discovery. The package version is read from the installed distribution
metadata, so ``pyproject.toml`` stays the only place it is written.
"""

from importlib.metadata import version

__version__: str = version("dyness-b3-bms-mqtt")
