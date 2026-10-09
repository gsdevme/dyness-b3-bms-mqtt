"""A manually driven clock for deterministic tests (TEST-11)."""


class FakeClock:
    """A clock that only moves when a test moves it.

    Calling the instance, or ``now()``, returns the current reading in seconds. It stands
    in for both the monotonic clock and the wall clock that components take by injection;
    ``sleep`` makes it usable wherever a sleep function is injected.
    """

    def __init__(self, start: float = 0.0) -> None:
        """Start the clock at ``start`` seconds."""
        self._now = float(start)

    def __call__(self) -> float:
        """Return the current reading."""
        return self._now

    def now(self) -> float:
        """Return the current reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``.

        Raises:
            ValueError: ``seconds`` is negative; a monotonic clock never goes back.

        """
        if seconds < 0:
            raise ValueError(f"cannot advance by a negative interval: {seconds!r}")
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        """Advance the clock instead of sleeping."""
        self.advance(seconds)
