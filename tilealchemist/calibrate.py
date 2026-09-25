#!/usr/bin/env python3
"""CLI entry point that proposes cost coefficients from a finished run's logs."""
import argparse
import glob
import json
import os
import sys

from tilealchemist.calibration import (
    MIN_SCORED_WORKERS,
    calibrate,
    correlation,
    parse_usage_lines,
)
from tilealchemist.cost import AXIS_SECONDS, WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.manifest import read_manifest, read_source_metadata

HELP = """Proposes the next run's cost coefficients from the last run's logs.

Reads the `usage:` lines a run's workers printed (see docs/ARCHITECTURE.md
"Measuring a run"), aggregates them as ratios rather than as means of
per-unit rates, and prints what it would change. It never edits cost.py:
--out writes a calibration.json for a caller to keep, and a human decides
what gets committed.

    grep -h '^usage:' logs/*.txt | tilealchemist-calibrate --expect-workers 128

    tilealchemist-calibrate --log 'logs/*.txt' --manifest-dir manifests \\
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
    parser.add_argument("--manifest-dir", default=None,
                         help="the run's manifests, to score predicted against measured "
                              "durations for the reviewed and the proposed coefficients")
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


def _score(manifest_dir, workers, axis):
    """Score predicted worker durations against the measured ones.

    Args:
        manifest_dir: The run's manifests, to price each worker's block from.
        workers: Parsed usage rows for scope "worker".
        axis: The per-axis seconds to price with.

    Returns:
        The correlation between predicted and measured durations, or None
        where a manifest is missing or too few workers reported to score.
    """
    predicted, measured = [], []
    for row in workers:
        path = os.path.join(manifest_dir, f"worker-{int(row['worker']):03d}.bin")
        if not os.path.exists(path):
            return None
        predicted.append(cost_weights(read_manifest(path), axis)[1])
        measured.append(float(row["wall_seconds"]))
    return correlation(predicted, measured)


def main():
    """Propose the next run's cost coefficients from a finished run's logs.

    Returns:
        0 on success, or 1 where no usage lines were found or only part of
        the run reported.
    """
    args = parse_args()
    rows = parse_usage_lines(_read_lines(args.log))
    result = calibrate(rows, runner_overhead_seconds=args.runner_overhead_seconds)

    seen = result.diagnostics["workers"]
    print(f"{seen} workers and {result.diagnostics['chunks']} chunks reported", file=sys.stderr)
    if args.expect_workers and seen != args.expect_workers:
        print(f"::error::{seen} of {args.expect_workers} workers reported; a partial run is a "
              f"biased sample and must not be calibrated from", file=sys.stderr)
        return 1
    if not seen:
        print("::error::no `usage:` lines found", file=sys.stderr)
        return 1

    measured = result.diagnostics["measured"]
    rows_out = [(name, getattr(AXIS_SECONDS, name), measured[name],
                 getattr(result.axis_seconds, name))
                for name in result.axis_seconds._fields]
    rows_out.append(("worker_setup_seconds", WORKER_SETUP_SECONDS,
                     measured["worker_setup_seconds"], result.worker_setup_seconds))
    print(f"{'coefficient':<22}{'reviewed':>14}{'measured':>14}{'proposed':>14}",
          file=sys.stderr)
    for name, reviewed, raw, proposed in rows_out:
        raw_text = "n/a" if raw is None else f"{raw:.4g}"
        print(f"{name:<22}{reviewed:>14.4g}{raw_text:>14}{proposed:>14.4g}", file=sys.stderr)

    runner = result.runner
    print(f"runner: peak RSS ~ {runner.rss_base / 2 ** 20:.0f} MiB + "
          f"{runner.rss_per_batch_byte:.2f} x peak batch bytes, and "
          f"{runner.bytes_per_output_tile:.0f} shard bytes per output tile", file=sys.stderr)

    share = result.diagnostics["decode_share"]
    if share is not None:
        print(f"decode is {share:.1%} of per-entry CPU, transform {1 - share:.1%}; the length "
              f"curve prefers DENSITY_EXPONENT "
              f"{result.diagnostics['best_density_exponent']}", file=sys.stderr)
    for note in result.notes:
        print(f"::warning title=calibration::{note}", file=sys.stderr)

    if args.manifest_dir:
        workers = [row for row in rows if row.get("scope") == "worker"]
        scores = {label: _score(args.manifest_dir, workers, axis)
                  for label, axis in (("reviewed", AXIS_SECONDS),
                                      ("proposed", result.axis_seconds))}
        unscored = f"not scored, under {MIN_SCORED_WORKERS} workers"
        for label, scored in scores.items():
            shown = unscored if scored is None else f"{scored:.3f}"
            print(f"predicted vs measured duration, {label}: {shown}", file=sys.stderr)
        if None not in scores.values() and scores["proposed"] < scores["reviewed"]:
            print(f"::warning title=calibration::the proposal ranks this run's own workers "
                  f"worse than the reviewed coefficients do ({scores['proposed']:.3f} against "
                  f"{scores['reviewed']:.3f}); do not commit it", file=sys.stderr)

    if args.out:
        payload = {"axis_seconds": result.axis_seconds._asdict(),
                   "worker_setup_seconds": result.worker_setup_seconds,
                   "density_exponent": result.density_exponent,
                   "runner": result.runner._asdict(),
                   "diagnostics": result.diagnostics, "notes": result.notes}
        if args.source:
            source = read_source_metadata(args.source)
            payload["source"] = {"url": source.url, "build": source.build,
                                  "schema": str(source.schema)}
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"wrote {args.out}; nothing in this repository reads it until a human commits "
              f"it as AXIS_SECONDS or passes it to prepare-shards", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
