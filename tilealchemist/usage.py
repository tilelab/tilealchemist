"""What a run actually cost, measured rather than predicted; see docs/ARCHITECTURE.md."""
import collections
import contextlib
import sys
import time

LENGTH_BUCKET_COUNT = 32


def _format(value):
    """Render one field value for a usage line.

    Args:
        value: The value to render.

    Returns:
        A fixed-point form for a float, and `str()` for anything else.
    """
    return f"{value:.6f}" if isinstance(value, float) else str(value)


def report(scope, **fields):
    """Print one greppable usage line.

    One line per scope, so that a whole run's budget is one `grep '^usage:'`
    over its job logs.

    Args:
        scope: What the line is about, such as "profile" or "worker".
        **fields: The measurements to print, as `name=value` pairs.

    Returns:
        The line as printed, so that a caller can also keep it for the job that
        collects every worker's measurements.
    """
    formatted = " ".join(f"{name}={_format(value)}" for name, value in fields.items())
    line = f"usage: scope={scope} {formatted}"
    print(line, file=sys.stderr)
    return line


class PhaseSeconds:
    """Wall-clock seconds per phase, exclusive: a nested phase's time is not its parent's."""

    def __init__(self):
        """Start with no phases recorded."""
        self.seconds = collections.defaultdict(float)
        self._nested = []

    @contextlib.contextmanager
    def phase(self, name):
        """Time a phase, for as long as the context stays open.

        Args:
            name: What to record the time under. Reopening a name adds to it.

        Yields:
            None; the phase's time is taken on the way out, less whatever nested
            phases took.
        """
        self._nested.append(0.0)
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.seconds[name] += elapsed - self._nested.pop()
            if self._nested:
                self._nested[-1] += elapsed

    def total(self):
        """The whole time recorded, across every phase.

        Returns:
            The seconds, which sum cleanly because the phases are exclusive.
        """
        return sum(self.seconds.values())

    def fields(self, suffix="_seconds"):
        """The phases as usage-line fields.

        Args:
            suffix: Appended to each phase's name.

        Returns:
            A mapping of field name to seconds, in phase-name order.
        """
        return {name + suffix: value for name, value in sorted(self.seconds.items())}


class TransformUsage:
    """What a transform cost, measured as it ran, for one chunk or a whole worker.

    A chunk's measurements are taken in the process that ran it and merged into
    the worker's own on the way back, so that a worker reports once rather than
    once per chunk; see docs/ARCHITECTURE.md "Measuring a run".

    Attributes:
        entries: Manifest entries walked.
        decode_calls: Tiles actually decoded, a duplicate not counted twice.
        decoded_bytes: Source bytes decoded.
        decode_seconds: Seconds spent decoding.
        profile_seconds: Seconds spent inside each profile, in profile order, so
            that a profile's own cost is attributable to it rather than to the
            run's profile set as a whole.
        profile_output_bytes: Output payload bytes each profile wrote, in the
            same order, counted once per output tile. Measured here rather than
            from the finished shard so it is payload alone -- no sqlite page
            overhead, and no gap tiles, which are a different size entirely and
            counted separately.
        output_tiles: Tiles written out, per profile: every profile is offered
            the same tiles.
        buckets: Per-bucket `[calls, bytes, decode]`, bucketed by the bit length
            of the tile, which is the shape the decode axes are fitted against.
    """

    def __init__(self, profile_count=0):
        """Start every measurement at zero.

        Args:
            profile_count: How many profiles the run builds, which fixes the
                length of `profile_seconds` for the run.
        """
        self.entries = 0
        self.decode_calls = 0
        self.decoded_bytes = 0
        self.decode_seconds = 0.0
        self.output_tiles = 0
        self.profile_seconds = [0.0] * profile_count
        self.profile_output_bytes = [0] * profile_count
        self.buckets = [[0, 0, 0.0] for _ in range(LENGTH_BUCKET_COUNT)]

    def add_decode(self, length, decode_seconds, profile_seconds):
        """Record one tile's decode, and what each profile spent on it.

        Args:
            length: The tile's source bytes.
            decode_seconds: Seconds spent decoding it.
            profile_seconds: Seconds each profile spent on it, in profile order.
        """
        self.decode_calls += 1
        self.decoded_bytes += length
        self.decode_seconds += decode_seconds
        for index, seconds in enumerate(profile_seconds):
            self.profile_seconds[index] += seconds
        bucket = self.buckets[min(length.bit_length(), LENGTH_BUCKET_COUNT - 1)]
        bucket[0] += 1
        bucket[1] += length
        bucket[2] += decode_seconds

    def merge(self, other):
        """Fold one chunk's measurements into these.

        Every field is a running total, so merging is addition throughout; the
        run's profile set is fixed, which is what lets `profile_seconds` add
        position by position.

        Args:
            other: The measurements to add, from a chunk this worker ran.
        """
        self.entries += other.entries
        self.decode_calls += other.decode_calls
        self.decoded_bytes += other.decoded_bytes
        self.decode_seconds += other.decode_seconds
        self.output_tiles += other.output_tiles
        for index, seconds in enumerate(other.profile_seconds):
            self.profile_seconds[index] += seconds
        for index, byte_count in enumerate(other.profile_output_bytes):
            self.profile_output_bytes[index] += byte_count
        for bucket, addend in zip(self.buckets, other.buckets):
            bucket[0] += addend[0]
            bucket[1] += addend[1]
            bucket[2] += addend[2]

    def transform_seconds(self):
        """What every profile spent together.

        Returns:
            The summed per-profile seconds.
        """
        return sum(self.profile_seconds)

    def length_histogram(self):
        """The per-length buckets, as one usage-line field value.

        Returns:
            `bits:calls:bytes:decode` per non-empty bucket, separated by `|`, or
            `-` where nothing was decoded at all.
        """
        return "|".join(
            f"{bits}:{count}:{byte_count}:{decode:.6f}"
            for bits, (count, byte_count, decode) in enumerate(self.buckets)
            if count) or "-"

    def fields(self):
        """These measurements as usage-line fields.

        The per-profile seconds are left out: they belong on the `scope=profile`
        line, which is already keyed by the profile they were measured on.

        Returns:
            A mapping of field name to value.
        """
        return {
            "entries": self.entries,
            "decode_calls": self.decode_calls,
            "decoded_bytes": self.decoded_bytes,
            "decode_seconds": self.decode_seconds,
            "output_tiles": self.output_tiles,
            "length_hist": self.length_histogram(),
        }
