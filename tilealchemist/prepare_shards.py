#!/usr/bin/env python3
"""CLI entry point for the planning step; the run itself is in shard_prep.py."""
import argparse

from tilealchemist.budgets import DEFAULT_MANIFEST_RAM_BYTES, DEFAULT_PEAK_BATCH_BYTES
from tilealchemist.calibration import DEFAULT_RUNNER, load_calibration_file
from tilealchemist.cost import AXIS_SECONDS
from tilealchemist.sizing import (
    DEFAULT_CONCURRENCY,
    DEFAULT_JOB_SECONDS,
    DEFAULT_WORKER_DISK_BYTES,
    DEFAULT_WORKER_RAM_BYTES,
    MATRIX_CELL_LIMIT,
    Limits,
    TAIL_SAFETY_FACTOR,
)
from tilealchemist.schemas import SchemaName
from tilealchemist.shard_prep import run_prepare
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.zoom import MAX_SUPPORTED_ZOOM, ZoomLevel

HELP = """The whole run's one-time planning step, before any shard worker starts.

Walks a source PMTiles archive's directory tree once and partitions the
resulting entries, plus computed gaps, into --worker-count contiguous
manifests, one per worker. docs/ARCHITECTURE.md ("Fetching") says why it is
structured this way.

Prints the layer's attribution on stdout; every log line goes to stderr.

    tilealchemist-prepare-shards --worker-count 128 --min-zoom 0 --max-zoom 14 \\
        --out-dir manifests/
"""


def zoom_level_type(value):
    zoom = int(value)  # A non-numeric value is argparse's own error to report.
    try:
        return ZoomLevel(zoom)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_SUPPORTED_ZOOM}") from None


def schema_type(value):
    try:
        return SchemaName(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be one of {', '.join(SchemaName)}") from None


def worker_count_type(value):
    if value == "auto":
        return value
    count = int(value)  # A non-numeric value is argparse's own error to report.
    if not 1 <= count <= MATRIX_CELL_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MATRIX_CELL_LIMIT}, the most cells GitHub Actions will "
            f"expand a matrix to, or \"auto\"")
    return count


def parse_args():
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-count", type=worker_count_type, default=128,
                         help=f"how many workers to split the run across (1-{MATRIX_CELL_LIMIT}, "
                              "default 128), or \"auto\" to pick the smallest multiple of "
                              "--concurrency whose worst worker stays inside every budget")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                         help="how many workers actually run at once (default "
                              f"{DEFAULT_CONCURRENCY}); \"auto\" only considers multiples of "
                              "it, so the last wave is not mostly empty")
    parser.add_argument("--job-seconds", type=float, default=DEFAULT_JOB_SECONDS,
                         help=f"a worker job's runtime limit (default {DEFAULT_JOB_SECONDS}); "
                              f"predicted seconds are charged against it at "
                              f"{TAIL_SAFETY_FACTOR:g}x, the factor by which the model "
                              "under-predicts the slow tail")
    parser.add_argument("--worker-ram-budget", type=int, default=DEFAULT_WORKER_RAM_BYTES,
                         help=f"a worker's usable memory (default {DEFAULT_WORKER_RAM_BYTES})")
    parser.add_argument("--worker-disk-budget", type=int, default=DEFAULT_WORKER_DISK_BYTES,
                         help="disk a worker's shards may take together (default "
                              f"{DEFAULT_WORKER_DISK_BYTES})")
    parser.add_argument("--min-zoom", type=zoom_level_type, default=ZoomLevel.Z0)
    parser.add_argument("--max-zoom", type=zoom_level_type, default=ZoomLevel.Z14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--axis-seconds", default=None,
                         help="calibration.json from tilealchemist-calibrate, whose coefficients "
                              "replace the reviewed ones in cost.py for this run; the "
                              "coefficients are profile-dependent, so a calibration belongs to "
                              "the profile set and archive it was measured on")
    parser.add_argument("--manifest-ram-budget", type=int, default=DEFAULT_MANIFEST_RAM_BYTES,
                         help="bytes of a worker's memory the manifest records themselves may "
                              f"take (default {DEFAULT_MANIFEST_RAM_BYTES}); a block is closed "
                              "once another group would break it, whatever the cost balance "
                              "says. 0 disables the cap")
    parser.add_argument("--peak-batch-budget", type=int, default=DEFAULT_PEAK_BATCH_BYTES,
                         help="bytes the largest single range request a worker makes may reach "
                              f"(default {DEFAULT_PEAK_BATCH_BYTES}); this is the peak, not the "
                              "block's byte sum, a worker dropping each batch before the next. "
                              "0 disables the cap")
    parser.add_argument("--attribution", default=None,
                         help="what the built layer credits, as a template in which "
                              "`{source}` stands for the attribution the archive declares "
                              "for itself; left off, the archive's own is carried through "
                              "unchanged")
    parser.add_argument("--source", choices=sorted(SOURCES), default="openfreemap",
                         help="where to resolve the PMTiles archive from (default openfreemap)")
    parser.add_argument("--source-url", default=None,
                         help="the PMTiles URL to use, required when --source static-url")
    parser.add_argument("--schema", type=schema_type, choices=list(SchemaName), default=None,
                         help="which schema the archive's tiles are in, required when --source "
                              "static-url and not accepted otherwise: every other source says "
                              "what its provider publishes (see sources/base.py)")
    args = parser.parse_args()

    try:
        args.axis_seconds, args.runner = (load_calibration_file(args.axis_seconds)
                                          if args.axis_seconds
                                          else (AXIS_SECONDS, DEFAULT_RUNNER))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"--axis-seconds: {error}")
    args.limits = Limits(job_seconds=args.job_seconds, ram_bytes=args.worker_ram_budget,
                          disk_bytes=args.worker_disk_budget, concurrency=args.concurrency,
                          tail_factor=TAIL_SAFETY_FACTOR)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    for name in ("manifest_ram_budget", "peak_batch_budget"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must not be negative")
    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed --max-zoom ({args.max_zoom})")
    # Discarded: built only to make a bad flag combination a usage error. Touches no network.
    try:
        resolve_source(args.source, args.source_url, args.schema)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    run_prepare(parse_args())


if __name__ == "__main__":
    main()
