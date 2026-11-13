"""Rate limiting for progress output: `update:` lines."""
import time


class UpdateLineThrottle:
    """Allows at most one line per interval.

    Attributes:
        interval: Minimum seconds between two lines.
        last_fired_at: Monotonic time of the last line, None before the first.
    """

    interval: float
    last_fired_at: float | None

    def __init__(self, interval: float, fire_immediately: bool = False) -> None:
        """Starts the throttle; the first line waits an interval unless told."""
        self.interval = interval
        self.last_fired_at = None if fire_immediately else time.monotonic()

    def due(self) -> bool:
        """Returns whether a line may go out now, and if so records it."""
        now: float = time.monotonic()
        if (self.last_fired_at is None
                or now - self.last_fired_at >= self.interval):
            self.last_fired_at = now
            return True
        return False
