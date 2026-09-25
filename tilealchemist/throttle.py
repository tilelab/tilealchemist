"""Rate limiter for a worker's `update:` lines; see docs/ARCHITECTURE.md."""
import threading
import time


class UpdateLineThrottle:
    """Lets a caller emit at most one line per interval.

    Shared between a worker's threads, which is why `due()` is the whole
    interface: it answers and records the answer under one lock, so two
    threads cannot both decide they are the one to log.

    Attributes:
        interval: Minimum seconds between two lines.
        last_fired_at: Monotonic time of the last line, or None if the next
            call may fire immediately.
        lock: Guards the read-and-update in `due()`.
    """

    def __init__(self, interval, fire_immediately=False):
        """Set up a throttle.

        Args:
            interval: Minimum seconds between two lines.
            fire_immediately: Whether the first `due()` may fire straight away
                rather than waiting out one interval first.
        """
        self.interval = interval
        self.last_fired_at = None if fire_immediately else time.monotonic()
        self.lock = threading.Lock()

    def due(self):
        """Claim the right to emit a line now.

        Returns:
            True if at least `interval` has passed since the last claim, in which
            case this call is recorded as the new last one; False otherwise.
        """
        with self.lock:
            now = time.monotonic()
            if self.last_fired_at is None or now - self.last_fired_at >= self.interval:
                self.last_fired_at = now
                return True
            return False
