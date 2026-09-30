#!/usr/bin/env python3
"""CLI entry point that proposes cost coefficients from a finished run's logs."""
import argparse
import glob
import json
import sys

from tilealchemist import axis_state
from tilealchemist.calibration import (
    measure_run,
    parse_usage_lines,
    proposal_lines,
)
from tilealchemist.manifest import read_source_metadata

HELP = """Proposes the next run's cost coefficients from the last run's logs.

Reads the `usage:` lines a run's workers printed (see docs/ARCHITECTURE.md
"Measuring a run"), aggregates them as ratios rather than as means of
per-unit rates, and prints what it would change, grouped by what each
coefficient belongs to: the archive, a profile, or neither. It never edits
cost.py and it never writes the state branch -- that is
`tilealchemist-merge-axes`, which a pipeline runs by itself. This is the
by-hand tool: --out writes a flat calibration.json for a caller to keep, and a
human decides what gets committed.

    grep -h '^usage:' logs/*.txt | tilealchemist-calibrate --expect-workers 128

    tilealchemist-calibrate --log 'logs/*.txt' \\
        --source manifests/source.json --out calibration.json
"""


def parse_args():
    """Parse this command's arguments.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", action="append", default=[],
                         help="file or glob of worker logs to read; repeatable, and stdin is "
                              "read when none is given")
    parser.add_argument("--expect-workers", type=int, default=0,
                         help="how many workers the run had; a calibration is refused unless "
                              "every one of them reported, a partial run being a biased sample")
    parser.add_argument("--runner-overhead-seconds", type=float, default=None,
                         help="seconds between a CI job starting and this worker's process "
                              "starting -- runner boot, artifact download, pip install. A "
                              "worker's own log cannot see it, so worker_setup_seconds is left "
                              "alone unless this is given")
    parser.add_argument("--source", default=None,
                         help="source.json from the same run, recorded in --out so a caller "
                              "can key the calibration by archive build and schema")
    parser.add_argument("--out", default=None,
                         help="where to write the proposed calibration as JSON; left off, "
                              "nothing is written and the proposal is printed only")
    return parser.parse_args()


def _read_lines(patterns):
    """Read the log lines named on the command line.

    Args:
        patterns: Files or globs to read, empty to read standard input.

    Returns:
        Every line read, in file-name order.
    """
    if not patterns:
        return sys.stdin.read().splitlines()
    lines = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) or [pattern]
        for path in matches:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(handle.read().splitlines())
    return lines


def main():
    """Propose the next run's cost coefficients from a finished run's logs.

    Returns:
        0 on success, or 1 where no usage lines were found or only part of
        the run reported.
    """
    args = parse_args()
    rows = parse_usage_lines(_read_lines(args.log))
    measurement = measure_run(rows, runner_overhead_seconds=args.runner_overhead_seconds)

    seen = measurement.diagnostics["workers"]
    print(f"{seen} workers and {measurement.diagnostics['profile_rows']} profile rows reported",
          file=sys.stderr)
    if args.expect_workers and seen != args.expect_workers:
        print(f"::error::{seen} of {args.expect_workers} workers reported; a partial run is a "
              f"biased sample and must not be calibrated from", file=sys.stderr)
        return 1
    if not seen:
        print("::error::no `usage:` lines found", file=sys.stderr)
        return 1

    for line in proposal_lines(measurement):
        print(line, file=sys.stderr)
    for note in measurement.notes:
        print(f"::warning title=calibration::{note}", file=sys.stderr)

    # What this one run alone would propose, which is what --out carries.
    document = axis_state.empty_document()
    axis_state.record_run(document, measurement)
    notes = []
    proposed, shared = axis_state.axis_seconds(document, measurement.source_key, notes)
    for note in notes:
        print(f"::warning title=calibration::{note}", file=sys.stderr)

    if args.out:
        payload = {"axis_seconds": proposed._asdict(),
                   "worker_setup_seconds": shared.worker_setup_seconds,
                   "profiles": {name: measured._asdict()
                                for name, measured in sorted(measurement.profiles.items())},
                   "source_key": measurement.source_key,
                   "diagnostics": measurement.diagnostics,
                   "notes": measurement.notes + notes}
        if args.source:
            source = read_source_metadata(args.source)
            payload["source"] = {"url": source.url, "build": source.build,
                                  "schema": str(source.schema)}
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"wrote {args.out}; nothing reads it until a human commits it as AXIS_SECONDS or "
              f"passes it to prepare-shards --axis-seconds. The pipeline's own path is "
              f"tilealchemist-merge-axes, which writes the state branch instead",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
