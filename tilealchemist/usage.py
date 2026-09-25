"""What a run actually cost, measured rather than predicted; see docs/ARCHITECTURE.md."""
import collections
import contextlib
import resource
import shutil
import sys
import time

LENGTH_BUCKET_COUNT = 32

FREE_DISK_WARNING_BYTES = 2 * 1024 * 1024 * 1024

# getrusage reports ru_maxrss in bytes on macOS and in kibibytes on Linux.
_MAX_RSS_SCALE = 1 if sys.platform == "darwin" else 1024


def peak_rss_bytes():
    """This process's peak resident set size.

    Returns:
        The peak RSS in bytes, whichever unit the platform reports it in.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAX_RSS_SCALE


def child_peak_rss_bytes():
    """The peak resident set size reached by this process's children.

    Returns:
        The largest peak RSS any one child reached, in bytes. The kernel
        reports children together, so a pool of them does not sum.
    """
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * _MAX_RSS_SCALE


def free_disk_bytes(path, threshold=FREE_DISK_WARNING_BYTES):
    """How much room is left where the shards are written.

    Warns below the threshold, because a shard that fills the disk fails
    part-way through an INSERT rather than before it starts.

    Args:
        path: A path on the filesystem to measure.
        threshold: The figure to warn below, in bytes.

    Returns:
        The free bytes.
    """
    free = shutil.disk_usage(path).free
    if free < threshold:
        print(f"::warning title=shard disk::{free} bytes free under {path!r}, under the "
              f"{threshold}-byte mark; a shard that fills the disk fails mid-INSERT",
              file=sys.stderr)
    return free


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
        scope: What the line is about, such as "chunk" or "worker".
        **fields: The measurements to print, as `name=value` pairs.
    """
    formatted = " ".join(f"{name}={_format(value)}" for name, value in fields.items())
    print(f"usage: scope={scope} {formatted}", file=sys.stderr)


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
    """What one chunk's transform cost, measured as it ran.

    Attributes:
        entries: Manifest entries walked.
        decode_calls: Tiles actually decoded, a duplicate not counted twice.
        decoded_bytes: Source bytes decoded.
        decode_seconds: Seconds spent decoding.
        transform_seconds: Seconds spent inside the profiles.
        output_tiles: Tiles written out.
        buckets: Per-bucket `[calls, bytes, decode, transform]`, bucketed by
            the bit length of the tile, which is the shape the cost model is
            fitted against.
    """

    def __init__(self):
        """Start every measurement at zero."""
        self.entries = 0
        self.decode_calls = 0
        self.decoded_bytes = 0
        self.decode_seconds = 0.0
        self.transform_seconds = 0.0
        self.output_tiles = 0
        self.buckets = [[0, 0, 0.0, 0.0] for _ in range(LENGTH_BUCKET_COUNT)]

    def add_decode(self, length, decode_seconds, transform_seconds):
        """Record one tile's decode and transform.

        Args:
            length: The tile's source bytes.
            decode_seconds: Seconds spent decoding it.
            transform_seconds: Seconds spent in the profiles over it.
        """
        self.decode_calls += 1
        self.decoded_bytes += length
        self.decode_seconds += decode_seconds
        self.transform_seconds += transform_seconds
        bucket = self.buckets[min(length.bit_length(), LENGTH_BUCKET_COUNT - 1)]
        bucket[0] += 1
        bucket[1] += length
        bucket[2] += decode_seconds
        bucket[3] += transform_seconds

    def length_histogram(self):
        """The per-length buckets, as one usage-line field value.

        Returns:
            `bits:calls:bytes:decode:transform` per non-empty bucket, separated
            by `|`, or `-` where nothing was decoded at all.
        """
        return "|".join(
            f"{bits}:{count}:{byte_count}:{decode:.6f}:{transform:.6f}"
            for bits, (count, byte_count, decode, transform) in enumerate(self.buckets)
            if count) or "-"

    def fields(self):
        """These measurements as usage-line fields.

        Returns:
            A mapping of field name to value.
        """
        return {
            "entries": self.entries,
            "decode_calls": self.decode_calls,
            "decoded_bytes": self.decoded_bytes,
            "decode_seconds": self.decode_seconds,
            "transform_seconds": self.transform_seconds,
            "output_tiles": self.output_tiles,
            "length_hist": self.length_histogram(),
        }


def report_chunk(usage, chunk_index, blob_bytes):
    """Print one chunk's usage line.

    Args:
        usage: That chunk's measurements.
        chunk_index: Which chunk this is, counting from one.
        blob_bytes: How many bytes the chunk was handed.
    """
    report("chunk", chunk=chunk_index, blob_bytes=blob_bytes,
           peak_rss=peak_rss_bytes(), **usage.fields())
