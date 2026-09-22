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
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAX_RSS_SCALE


def child_peak_rss_bytes():
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * _MAX_RSS_SCALE


def free_disk_bytes(path, threshold=FREE_DISK_WARNING_BYTES):
    free = shutil.disk_usage(path).free
    if free < threshold:
        print(f"::warning title=shard disk::{free} bytes free under {path!r}, under the "
              f"{threshold}-byte mark; a shard that fills the disk fails mid-INSERT",
              file=sys.stderr)
    return free


def _format(value):
    return f"{value:.6f}" if isinstance(value, float) else str(value)


def report(scope, **fields):
    """One greppable line, so a whole run's budget is one `grep '^usage:'` over its job logs."""
    formatted = " ".join(f"{name}={_format(value)}" for name, value in fields.items())
    print(f"usage: scope={scope} {formatted}", file=sys.stderr)


class PhaseSeconds:
    """Wall-clock seconds per phase, exclusive: a nested phase's time is not its parent's."""

    def __init__(self):
        self.seconds = collections.defaultdict(float)
        self._nested = []

    @contextlib.contextmanager
    def phase(self, name):
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
        return sum(self.seconds.values())

    def fields(self, suffix="_seconds"):
        return {name + suffix: value for name, value in sorted(self.seconds.items())}


class TransformUsage:

    def __init__(self):
        self.entries = 0
        self.decode_calls = 0
        self.decoded_bytes = 0
        self.decode_seconds = 0.0
        self.transform_seconds = 0.0
        self.output_tiles = 0
        self.buckets = [[0, 0, 0.0, 0.0] for _ in range(LENGTH_BUCKET_COUNT)]

    def add_decode(self, length, decode_seconds, transform_seconds):
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
        return "|".join(
            f"{bits}:{count}:{byte_count}:{decode:.6f}:{transform:.6f}"
            for bits, (count, byte_count, decode, transform) in enumerate(self.buckets)
            if count) or "-"

    def fields(self):
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
    report("chunk", chunk=chunk_index, blob_bytes=blob_bytes,
           peak_rss=peak_rss_bytes(), **usage.fields())
