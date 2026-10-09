#!/usr/bin/env python3
"""CLI entry point for one worker; the run itself is in shard_worker.py."""
import argparse
import os

from tilealchemist.fetch_batching import DEFAULT_MAX_FETCH_HOLE
from tilealchemist.profiles import load_profile
from tilealchemist.shard_worker import run_worker
from tilealchemist.transform import DEFAULT_REPORT_INTERVAL

HELP = """One worker: every profile's part, built from one shard.

docs/ARCHITECTURE.md "Fetching" and "Parallelism" say what this does and why;
docs/PROFILES.md says what a --profile computes per tile.

    tilealchemist-build-shard --worker-index 0 --profile ./my_profile.py \\
        --manifest manifests/worker-000.bin --source manifests/source.json \\
        --out my-profile-part-0.pmtiles

    tilealchemist-build-shard --worker-index 0 \\
        --profile ./my_profile.py,./other_profile.py \\
        --manifest manifests/worker-000.bin --source manifests/source.json \\
        --out my-profile-part-0.pmtiles,other-profile-part-0.pmtiles
"""

# Shorter than --report-interval because the download phase it covers is
# shorter.
DEFAULT_DOWNLOAD_REPORT_INTERVAL = 15.0


def parse_args():
    """Parse and check this worker's arguments.

    The profiles are loaded here, so that a bad --profile fails as a usage
    error rather than once the download is already paid for.

    Returns:
        The parsed arguments, with --profile and --out split into matching
        lists and the profile classes loaded onto them.
    """
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument(
        "--manifest", required=True,
        help="this worker's manifest file from prepare_shards.py")
    parser.add_argument(
        "--source", required=True,
        help="source.json written by prepare_shards.py, which names the "
             "archive, the schema its tiles are in, and the zoom range walked")
    parser.add_argument(
        "--out", required=True,
        help="comma-separated output .pmtiles part path(s), one per "
             "--profile, matched by position; a profile that writes no tile "
             "gets no file")
    parser.add_argument(
        "--usage-out", default=None,
        help="where to write this worker's `usage:` lines, for the "
             "merge-axes job to collect them as an artifact; left off, they "
             "go to the log alone")
    parser.add_argument(
        "--profile", required=True,
        help="comma-separated path(s) to a profile's .py file to apply, "
             "e.g. \"./my_profile.py\" or "
             "\"./my_profile.py,./other_profile.py\"")
    parser.add_argument(
        "--report-interval", type=float, default=DEFAULT_REPORT_INTERVAL,
        help="seconds between throttled transform-progress updates "
             "(default 60; download-progress updates have their own "
             "--download-report-interval)")
    parser.add_argument(
        "--download-report-interval", type=float,
        default=DEFAULT_DOWNLOAD_REPORT_INTERVAL,
        help="seconds between throttled download-progress updates (default "
             "15, shorter than --report-interval because the download phase "
             "it covers is itself shorter)")
    parser.add_argument(
        "--max-fetch-hole", type=int, default=DEFAULT_MAX_FETCH_HOLE,
        help="how many bytes of archive that this worker does not read may "
             "sit between two of its entries before the batch is split into "
             "another range request (default "
             f"{DEFAULT_MAX_FETCH_HOLE}); such holes come from PMTiles dedup, "
             "and get wide at a high --min-zoom")
    parser.add_argument(
        "--transform-processes", type=int, default=os.cpu_count() or 1,
        help="parallel processes for the CPU-bound transform phase "
             "(default: all available cores; 1 disables pooling and runs "
             "inline, same as before this flag existed)")
    args = parser.parse_args()

    if args.max_fetch_hole < 0:
        parser.error(f"--max-fetch-hole ({args.max_fetch_hole}) must not be "
                     f"negative")

    profile_paths = args.profile.split(",")
    out_paths = args.out.split(",")
    if len(profile_paths) != len(out_paths):
        parser.error(f"--profile has {len(profile_paths)} entries but --out "
                     f"has {len(out_paths)}; they must match 1:1")
    try:
        profile_classes = [load_profile(path) for path in profile_paths]
    except ValueError as error:
        parser.error(str(error))

    args.profile = profile_paths
    args.profile_classes = profile_classes
    args.out = out_paths

    return args


def main():
    """Run one worker."""
    run_worker(parse_args())


if __name__ == "__main__":
    main()
