#!/usr/bin/env python3
"""CLI entry point for the planning step; the run itself is in shard_prep.py."""
import argparse

from tilealchemist.axis_state import read_state_file
from tilealchemist.calibration import load_calibration_file
from tilealchemist.cost import AXIS_SECONDS
from tilealchemist.profiles import load_profile
from tilealchemist.sizing import (
    DEFAULT_CONCURRENCY,
    DEFAULT_JOB_SECONDS,
    MATRIX_CELL_LIMIT,
    Limits,
    TAIL_SAFETY_FACTOR,
    WORKER_SCALE_FACTOR,
)
from tilealchemist.schemas import SchemaName
from tilealchemist.shard_prep import run_prepare
from tilealchemist.sources import SOURCES, resolve_source
from tilealchemist.zoom import MAX_SUPPORTED_ZOOM, ZoomLevel

HELP = """The whole run's one-time planning step, before any worker starts.

Walks a source PMTiles archive's directory tree once and partitions the
resulting entries, plus computed gaps, into contiguous manifests, one per
worker. docs/ARCHITECTURE.md ("Fetching") says why it is structured this
way.

The worker count is never an input: the run partitions at --concurrency times
--worker-scale and doubles until its worst worker fits every limit (see
"Sizing a run"). The manifests it writes are the only truth about how many
workers there are.

Prints the layer's attribution on stdout; every log line goes to stderr.

    tilealchemist-prepare-shards --min-zoom 0 --max-zoom 14 \\
        --out-dir manifests/
"""


def zoom_level_type(value):
    """Parse a zoom level from the command line.

    Args:
        value: The flag's raw text.

    Returns:
        That value as a ZoomLevel.

    Raises:
        argparse.ArgumentTypeError: If it falls outside the supported range.
    """
    zoom = int(value)  # A non-numeric value is argparse's own error to report.
    try:
        return ZoomLevel(zoom)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_SUPPORTED_ZOOM}") from None


def schema_type(value):
    """Parse a schema name from the command line.

    Args:
        value: The flag's raw text.

    Returns:
        That value as a SchemaName.

    Raises:
        argparse.ArgumentTypeError: If no such schema is known.
    """
    try:
        return SchemaName(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be one of {', '.join(SchemaName)}") from None


def parse_args():
    """Parse and check this command's arguments.

    Every check a flag combination can fail is made here, so that a bad call
    fails as a usage error rather than part-way into the run.

    Returns:
        The parsed arguments, with the calibration loaded and the run's
        limits assembled onto them.
    """
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help="how many workers actually run at once (default "
             f"{DEFAULT_CONCURRENCY}). Times --worker-scale, it is the worker "
             "count the run tries first: it doubles from there until its "
             "worst worker fits every limit, stopping at "
             f"{MATRIX_CELL_LIMIT}, the most cells GitHub Actions will expand "
             "a matrix to. Every count tried is a multiple of this, so the "
             "last wave is not mostly empty")
    parser.add_argument(
        "--worker-scale", type=int, default=WORKER_SCALE_FACTOR,
        help="how many workers each concurrency lane gets at least (default "
             f"{WORKER_SCALE_FACTOR}). GitHub starts a queued worker as soon "
             "as a lane frees, so a lane that drew a fast runner works "
             "through more of them and runner speed evens out across the run "
             "instead of the slowest runner setting its end. 1 gives one "
             "worker per lane")
    parser.add_argument(
        "--job-seconds", type=float, default=DEFAULT_JOB_SECONDS,
        help=f"a worker job's runtime limit (default {DEFAULT_JOB_SECONDS}); "
             f"predicted seconds are charged against it at "
             f"{TAIL_SAFETY_FACTOR:g}x, to leave room for the tail the model "
             "cannot see -- content complexity, which no manifest entry "
             "exposes")
    parser.add_argument(
        "--profile", default=None,
        help="comma-separated path(s) to the profile .py files this run will "
             "build, the same value build-shard is given. Their per-tile "
             "seconds are what make the prediction profile-specific; without "
             "it a run is costed as if it passed tiles through unchanged")
    parser.add_argument("--min-zoom", type=zoom_level_type,
                        default=ZoomLevel.Z0)
    parser.add_argument("--max-zoom", type=zoom_level_type,
                        default=ZoomLevel.Z14)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--axis-seconds", default=None,
        help="calibration.json from tilealchemist-calibrate, whose "
             "coefficients replace the reviewed ones in cost.py for this "
             "run; the coefficients are profile-dependent, so a calibration "
             "belongs to the profile set and archive it was measured on")
    parser.add_argument(
        "--axis-state", default=None,
        help="state/axes.json off the state branch, as "
             "tilealchemist-merge-axes writes it. Its coefficients are the "
             "median of the last few runs and replace both the reviewed ones "
             "and each profile's declared per-tile figures, for the archive "
             "this run reads. A missing file is not an error: the first run "
             "has nothing measured yet")
    parser.add_argument(
        "--block-state", default=None,
        help="the state branch's state/blocks directory, as "
             "tilealchemist-merge-axes writes it: the profiles' measured "
             "seconds per tile block, one file per archive and profile set. "
             "A measured block is costed by the median of its own runs "
             "instead of by the profiles' per-tile seconds. A missing "
             "directory or file is not an error: nothing has been measured "
             "yet")
    parser.add_argument(
        "--attribution", default=None,
        help="what the built layer credits, as a template in which "
             "`{source}` stands for the attribution the archive declares "
             "for itself; left off, the archive's own is carried through "
             "unchanged")
    parser.add_argument(
        "--source", choices=sorted(SOURCES), default="openfreemap",
        help="where to resolve the PMTiles archive from (default "
             "openfreemap)")
    parser.add_argument(
        "--source-url", default=None,
        help="the PMTiles URL to use, required when --source static-url")
    parser.add_argument(
        "--schema", type=schema_type, choices=list(SchemaName), default=None,
        help="which schema the archive's tiles are in, required when "
             "--source static-url and not accepted otherwise: every other "
             "source says what its provider publishes (see sources/base.py)")
    args = parser.parse_args()

    try:
        args.axis_seconds = (load_calibration_file(args.axis_seconds)
                             if args.axis_seconds else AXIS_SECONDS)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"--axis-seconds: {error}")
    try:
        args.axis_state_document = read_state_file(args.axis_state)
    except (OSError, ValueError) as error:
        parser.error(f"--axis-state: {error}")
    args.limits = Limits(job_seconds=args.job_seconds,
                         concurrency=args.concurrency,
                         tail_factor=TAIL_SAFETY_FACTOR,
                         worker_scale=args.worker_scale)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.worker_scale < 1:
        parser.error("--worker-scale must be at least 1")
    try:
        args.profiles = ([load_profile(path)()
                          for path in args.profile.split(",")]
                         if args.profile else None)
    except (OSError, ValueError, ImportError, AttributeError,
            TypeError) as error:
        parser.error(f"--profile: {error}")
    if args.min_zoom > args.max_zoom:
        parser.error(f"--min-zoom ({args.min_zoom}) must not exceed "
                     f"--max-zoom ({args.max_zoom})")
    # Discarded: built only to make a bad flag combination a usage error.
    # It touches no network.
    try:
        resolve_source(args.source, args.source_url, args.schema)
    except ValueError as error:
        parser.error(str(error))
    return args


def main():
    """Run the planning step."""
    run_prepare(parse_args())


if __name__ == "__main__":
    main()
