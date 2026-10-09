"""What a run actually cost, measured rather than predicted.

See docs/ARCHITECTURE.md.
"""
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


def report(scope, *, echo=True, **fields):
    """Print one greppable usage line.

    One line per scope, so that a whole run's budget is one `grep '^usage:'`
    over its job logs.

    Args:
        scope: What the line is about, such as "profile" or "worker".
        echo: Whether to print it at all. A line too long to be worth reading
            in a log is only returned, for the usage file.
        **fields: The measurements to print, as `name=value` pairs.

    Returns:
        The line as printed, so that a caller can also keep it for the job that
        collects every worker's measurements.
    """
    formatted = " ".join(f"{name}={_format(value)}"
                         for name, value in fields.items())
    line = f"usage: scope={scope} {formatted}"
    if echo:
        print(line, file=sys.stderr)
    return line


class PhaseSeconds:
    """Wall-clock seconds per phase, exclusive of any nested phase's time."""

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
        return {name + suffix: value
                for name, value in sorted(self.seconds.items())}


class ShardUsage:
    """What a shard's decode and profiles cost, for the shard or one chunk.

    Only what is measured inside the transform pool's processes is kept here,
    since the worker cannot see it any other way: a chunk measures itself in
    the process that ran it, and is merged into the shard's own on the way
    back, so that a worker reports once rather than once per chunk; see
    docs/ARCHITECTURE.md "Measuring a run". Everything the worker can see for
    itself -- its phases, fetched bytes and tile counts -- it reports directly.

    Attributes:
        buckets: Per-bucket `[calls, bytes, decode]`, bucketed by the bit length
            of the tile, which is the shape the decode axes are fitted against.
            Every decode is counted here and nowhere else: its calls, bytes
            and seconds in total are these summed, a duplicate not counted
            twice.
        block_seconds: Seconds every profile spent together, per home tile
            block -- the block of the entry that decoded the blob, as
            `home_blocks()` defines it.
        block_bytes: Output payload bytes every profile wrote together, per
            home tile block, a deduplicated tile's included under the blob's
            home rather than its own. A block that wrote nothing is recorded
            at zero, which is a measurement, not a hole.
    """

    def __init__(self):
        """Start every measurement at zero."""
        self.buckets = [[0, 0, 0.0] for _ in range(LENGTH_BUCKET_COUNT)]
        self.block_seconds = {}
        self.block_bytes = {}

    def add_block(self, block, seconds):
        """Charge profile seconds to one tile block.

        Args:
            block: The block's key, as `home_blocks()` gives it.
            seconds: The seconds to add.
        """
        self.block_seconds[block] = self.block_seconds.get(block, 0.0) + seconds

    def add_block_bytes(self, block, byte_count):
        """Charge written output bytes to one tile block.

        Args:
            block: The block's key, as `home_blocks()` gives it.
            byte_count: The bytes to add, zero to record the block as walked.
        """
        self.block_bytes[block] = self.block_bytes.get(block, 0) + byte_count

    def add_decode(self, length, decode_seconds, profile_seconds, block):
        """Record one tile's decode, and what the profiles spent on it.

        Args:
            length: The tile's source bytes.
            decode_seconds: Seconds spent decoding it.
            profile_seconds: Seconds every profile spent on it together.
            block: The tile block the profiles' seconds are charged to.
        """
        self.add_block(block, profile_seconds)
        bucket = self.buckets[min(length.bit_length(), LENGTH_BUCKET_COUNT - 1)]
        bucket[0] += 1
        bucket[1] += length
        bucket[2] += decode_seconds

    def merge(self, other):
        """Fold one chunk's measurements into these.

        Every field is a running total, so merging is addition throughout.

        Args:
            other: The measurements to add, from a chunk this worker ran.
        """
        for bucket, addend in zip(self.buckets, other.buckets):
            bucket[0] += addend[0]
            bucket[1] += addend[1]
            bucket[2] += addend[2]
        for block, seconds in other.block_seconds.items():
            self.add_block(block, seconds)
        for block, byte_count in other.block_bytes.items():
            self.add_block_bytes(block, byte_count)

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

        The per-block seconds and bytes appear here summed over the blocks;
        each block's own figures go on the `scope=blocks` line. The profiles'
        seconds are named `profile_seconds` rather than `transform_seconds`,
        which is the worker's wall-clock phase of that name.

        Returns:
            A mapping of field name to value.
        """
        return {
            "profile_seconds": sum(self.block_seconds.values()),
            "output_bytes": sum(self.block_bytes.values()),
            "length_hist": self.length_histogram(),
        }
