#!/usr/bin/env python3
"""CLI entry point that merges every worker's measurements into the shared axis state."""
import argparse
import glob
import json
import os
import pathlib
import sys

from tilealchemist import axis_state
from tilealchemist.calibration import (
    MIN_SCORED_WORKERS,
    measure_run,
    parse_usage_lines,
    proposal_lines,
    score_axes,
    worker_rows,
)
from tilealchemist.cost import AXIS_SECONDS
from tilealchemist.manifest import read_source_metadata
from tilealchemist.state_branch import DEFAULT_BRANCH, ensure_branch, update_json

DEFAULT_STATE_PATH = "state/axes.json"

HELP = """Merges every worker's `usage:` lines into the shared axis state.

One writer for the whole run, the way tiledistillery's record-timings job is
(see docs/ARCHITECTURE.md "Measuring a run"). It fits what the run measured,
splits it by what each coefficient belongs to -- the archive, a profile, or
neither -- and appends each to its own history on the state branch, where the
next run reads the median of the last few.

It pushes nothing unless every worker reported: a partial run is a biased
sample, because the workers that failed are the expensive ones.

    tilealchemist-merge-axes --usage-dir usage --expect-workers 128 \\
        --source manifests/source.json --repo "$GITHUB_REPOSITORY" \\
        --token "$GH_TOKEN"

    tilealchemist-merge-axes --usage-dir usage --dry-run
"""


def parse_args():
    """Parse this command's arguments.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usage-dir", default=None,
                         help="directory of the workers' uploaded usage files, searched "
                              "recursively: each worker's artifact lands in its own subdirectory")
    parser.add_argument("--log", action="append", default=[],
                         help="file or glob of worker logs to read instead; repeatable, and "
                              "stdin is read when neither this nor --usage-dir is given")
    parser.add_argument("--expect-workers", type=int, default=0,
                         help="how many workers the run had; nothing is pushed unless every one "
                              "of them reported, a partial run being a biased sample")
    parser.add_argument("--source", default=None,
                         help="source.json from the same run, read for the build label recorded "
                              "beside the archive's coefficients")
    parser.add_argument("--runner-overhead-seconds", type=float, default=None,
                         help="seconds between a CI job starting and a worker's process starting "
                              "-- runner boot, artifact download, pip install. No worker's log "
                              "can see it, so worker_setup_seconds is left unmeasured without it")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                         help="the owner/name whose state branch holds the axes (default: "
                              "$GITHUB_REPOSITORY). The caller's repository, not tilealchemist's: "
                              "the coefficients belong to the profile set and archive measured")
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                         help="token to write the state branch with (default: $GH_TOKEN)")
    parser.add_argument("--state-branch", default=DEFAULT_BRANCH,
                         help=f"branch the state lives on (default {DEFAULT_BRANCH}), created as "
                              f"an empty orphan if it is not there yet")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH,
                         help=f"the state file's path on that branch (default {DEFAULT_STATE_PATH})")
    parser.add_argument("--manifest-dir", default=None,
                         help="the run's manifests, to score what the new coefficients predict "
                              "against what the run measured. Ranking worse than the reviewed "
                              "ones refuses the push: a fit can improve every ratio and still be "
                              "a worse model")
    parser.add_argument("--out", default=None,
                         help="also write the merged document here, for a caller that wants it "
                              "as a job artifact")
    parser.add_argument("--dry-run", action="store_true",
                         help="fit and print, push nothing")
    return parser.parse_args()


def read_lines(usage_dir, patterns):
    """Read every usage line the run produced.

    Args:
        usage_dir: Directory of uploaded usage files, searched recursively, or
            None. Missing or empty means nothing was uploaded.
        patterns: Files or globs to read instead. Empty, with no directory
            either, reads standard input.

    Returns:
        Every line read, in path order.
    """
    lines = []
    if usage_dir and pathlib.Path(usage_dir).is_dir():
        for path in sorted(pathlib.Path(usage_dir).rglob("*")):
            if path.is_file():
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(handle.read().splitlines())
    if not lines and not usage_dir and not patterns:
        return sys.stdin.read().splitlines()
    return lines


def _outranked(args, rows, measurement):
    """Whether the new coefficients predict this run worse than the reviewed ones.

    Args:
        args: The parsed command line, read for its manifest directory.
        rows: Parsed usage rows for the whole run.
        measurement: The run's RunMeasurement.

    Returns:
        True where the proposal must not be pushed. Without --manifest-dir, or
        with too few workers to score, nothing is claimed either way.
    """
    if not args.manifest_dir:
        return False
    notes = []
    proposed = axis_state.axis_seconds(_document_of(measurement),
                                        measurement.source_key, notes)[0]
    scores = {label: score_axes(args.manifest_dir, worker_rows(rows), axis)
              for label, axis in (("reviewed", AXIS_SECONDS), ("proposed", proposed))}
    for label, scored in scores.items():
        shown = (f"not scored, under {MIN_SCORED_WORKERS} workers" if scored is None
                 else f"{scored:.3f}")
        print(f"predicted vs measured duration, {label}: {shown}", file=sys.stderr)
    if None in scores.values():
        return False
    if scores["proposed"] < scores["reviewed"]:
        print(f"::error::the fit ranks this run's own workers worse than the reviewed "
              f"coefficients do ({scores['proposed']:.3f} against {scores['reviewed']:.3f}); "
              f"not pushing it", file=sys.stderr)
        return True
    return False


def _document_of(measurement):
    """This run's measurements alone, as a state document.

    Args:
        measurement: The run's RunMeasurement.

    Returns:
        A document holding only this run, for scoring what it would propose
        before it is merged with the history.
    """
    document = axis_state.empty_document()
    axis_state.record_run(document, measurement)
    return document


def main():
    """Merge the run's measurements into the state branch.

    Returns:
        0 on success, or 1 where no usage lines were found, only part of the
        run reported, or a push was asked for without somewhere to push to.
    """
    args = parse_args()
    rows = parse_usage_lines(read_lines(args.usage_dir, args.log))
    measurement = measure_run(rows, runner_overhead_seconds=args.runner_overhead_seconds)

    seen = measurement.diagnostics["workers"]
    print(f"{seen} workers and {measurement.diagnostics['profile_rows']} profile rows reported",
          file=sys.stderr)
    if not seen:
        print("::error::no `usage:` lines found; nothing to merge", file=sys.stderr)
        return 1
    if args.expect_workers and seen != args.expect_workers:
        print(f"::error::{seen} of {args.expect_workers} workers reported; a partial run is a "
              f"biased sample and must not be merged into the state", file=sys.stderr)
        return 1

    for line in proposal_lines(measurement):
        print(line, file=sys.stderr)
    for note in measurement.notes:
        print(f"::warning title=merge-axes::{note}", file=sys.stderr)

    if _outranked(args, rows, measurement):
        return 1

    build = read_source_metadata(args.source).build if args.source else None

    def mutate(document):
        """Append this run's measurements to whatever the branch already held.

        Args:
            document: The state document as read.

        Returns:
            The document to write back.
        """
        if not document:
            document = axis_state.empty_document()
        for line in axis_state.record_run(document, measurement, build=build):
            print(line, file=sys.stderr)
        return document

    if args.dry_run or not (args.repo and args.token):
        document = mutate(axis_state.empty_document())
        if not args.dry_run:
            print("::error::--repo and --token are needed to push; pass --dry-run to fit only",
                  file=sys.stderr)
        if args.out:
            _write_out(args.out, document)
        return 0 if args.dry_run else 1

    ensure_branch(args.repo, args.token, args.state_branch)
    document = update_json(
        args.repo, args.token, args.state_branch, args.state_path, mutate,
        message=f"record axes from {seen} workers on {measurement.source_key}")
    depth = axis_state.history_depth(document)
    print(f"pushed to {args.repo}@{args.state_branch}:{args.state_path}; the shallowest "
          f"coefficient now rests on {depth} run(s) of {axis_state.HISTORY_LENGTH}",
          file=sys.stderr)
    if args.out:
        _write_out(args.out, document)
    return 0


def _write_out(path, document):
    """Write the merged document somewhere a job can upload it.

    Args:
        path: Where to write.
        document: The document to write.
    """
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
    print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
