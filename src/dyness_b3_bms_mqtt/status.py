"""Shared status vocabularies of ``docs/specs/03-health-and-recovery.md``.

The health machine produces these values and the telemetry store and publisher consume
them, so they live in a module of their own that imports nothing from either side. Every
member's value is the lower-case string that is logged and published.
"""

from enum import StrEnum


class ModuleStatus(StrEnum):
    """The freshness status of one module (HEALTH-05), checked in member order."""

    OK = "ok"
    CELLS_STALE = "cells_stale"
    SILENT = "silent"


class HealthState(StrEnum):
    """The states of the health and recovery machine (03-health-and-recovery.md section 4)."""

    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    RECOVERING = "recovering"
    DEAD = "dead"


RUNNING_STATES = frozenset({HealthState.STARTING, HealthState.HEALTHY, HealthState.DEGRADED})
"""States in which a module can be online; in every other state each module is SILENT."""
