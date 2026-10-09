"""UTC timestamps for values that are published or persisted.

Ages and timers use the injected monotonic clock (HEALTH-01); wall-clock seconds are only
ever turned into text here, for fields such as ``updated`` in ``recovery.json``.
"""

import math
from datetime import UTC, datetime

_ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


def iso_utc(wall: float) -> str:
    """Return Unix time ``wall`` as ``YYYY-MM-DDTHH:MM:SSZ``, truncated to the whole second."""
    return datetime.fromtimestamp(math.floor(wall), UTC).strftime(_ISO_UTC)
