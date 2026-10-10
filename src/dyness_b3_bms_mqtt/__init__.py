"""Dyness B3 BMS to MQTT bridge.

Reads a stack of Dyness B3 battery modules over CAN and publishes their telemetry to MQTT
with Home Assistant discovery.

``__version__`` is the only place the version is written. ``pyproject.toml`` declares the
version dynamic and hatchling reads it from this file at build time, and release-please's
python strategy rewrites the line on each release. Its pattern is ``__version__ = "X.Y.Z"``,
so the line carries no type annotation.
"""

__version__ = "0.1.2"
