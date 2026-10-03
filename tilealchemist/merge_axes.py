#!/usr/bin/env python3
"""CLI entry point that merges every worker's measurements into the shared axis state."""
import argparse
import glob
import json
import os
import pathlib
import re
import statistics
import sys
from datetime import datetime

from tilealchemist import axis_state, block_state
from tilealchemist.calibration import (
    measure_run,
    parse_usage_lines,
    proposal_lines,
)
from tilealchemist.manifest import read_source_metadata
from tilealchemist.state_branch import DEFAULT_BRANCH, ensure_branch, request, update_json

DEFAULT_STATE_PATH = "state/axes.json"

# A worker job as `_pipeline.yml` names it, its matrix index being the manifest it read.
WORKER_JOB = re.compile(r"build-shards \((\d+)\)$")

JOBS_PER_PAGE = 100

HELP = """Merges every worker's `usage:` lines into the shared axis state.

One writer for the whole run, the way tiledistillery's record-timings job is
(see docs/ARCHITECTURE.md "Measuring a run"). It fits what the run measured,
splits it by what each coefficient belongs to -- the archive, or neither --
and appends each to its own history on the state branch, where the next run
reads the median of the last few. What the profiles cost -- their seconds and
their written bytes per tile block -- goes the same way, into a file of its
own per archive and profile set (see "Measured tile blocks").

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
                         help="seconds a worker job spends outside the worker's process "
                              "-- runner boot, artifact download, pip install, uploads. No "
                              "worker's log can see it; left off, it is read off the run's job "
                              "timings, which needs `actions: read`")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"),
                         help="the run whose worker jobs are timed for the runner overhead "
                              "(default: $GITHUB_RUN_ID)")
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                         help="which attempt of that run (default: $GITHUB_RUN_ATTEMPT)")
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
    parser.add_argument("--block-state-dir", default=block_state.DEFAULT_BLOCK_STATE_DIR,
                         help="directory on that branch holding the per-tile-block seconds and "
                              "written bytes, one "
                              "file per archive and profile set (default "
                              f"{block_state.DEFAULT_BLOCK_STATE_DIR})")
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


def measure_runner_overhead(repo, token, run_id, attempt, rows):
    """Time what each worker job spent outside the worker's own process.

    The job's span on GitHub, less the `wall_seconds` its process reported,
    is everything the process could not see: runner boot, checkout, pip, the
    manifest download before it and the uploads after it. All of that is paid
    once per worker whatever its block holds, which is what
    `worker_setup_seconds` charges. The median across workers, so one runner
    that queued for a disk does not set it.

    Args:
        repo: The owner/name the run belongs to.
        token: A token that can read the run's jobs, which takes
            `actions: read`.
        run_id: The run to time.
        attempt: Which attempt of it.
        rows: Parsed usage rows for the whole run, read for each worker's
            `wall_seconds`.

    Returns:
        The median overhead in seconds, or None where the jobs could not be
        read or none of them matched a reporting worker.
    """
    walls = {int(row["worker"]): float(row["wall_seconds"]) for row in rows
             if row.get("scope") == "worker" and "wall_seconds" in row}
    overheads, page = [], 1
    while True:
        response = request("GET", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                           token, params={"per_page": JOBS_PER_PAGE, "page": page})
        if response.status_code != 200:
            print(f"::warning title=merge-axes::cannot read this run's job timings "
                  f"({response.status_code}), so worker_setup_seconds stays unmeasured; the "
                  f"calling job needs `actions: read` beside `contents: write`", file=sys.stderr)
            return None
        jobs = response.json().get("jobs", [])
        for job in jobs:
            match = WORKER_JOB.search(job.get("name", ""))
            if (not match or job.get("conclusion") != "success"
                    or int(match.group(1)) not in walls):
                continue
            span = (_timestamp(job["completed_at"]) - _timestamp(job["started_at"])).total_seconds()
            overheads.append(span - walls[int(match.group(1))])
        if len(jobs) < JOBS_PER_PAGE:
            break
        page += 1
    return statistics.median(overheads) if overheads else None


def _timestamp(value):
    """Read one of the API's ISO 8601 timestamps.

    Args:
        value: The timestamp, `Z`-suffixed as the API writes it.

    Returns:
        The aware datetime.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main():
    """Merge the run's measurements into the state branch.

    Returns:
        0 on success, or 1 where no usage lines were found, only part of the
        run reported, or a push was asked for without somewhere to push to.
    """
    args = parse_args()
    rows = parse_usage_lines(read_lines(args.usage_dir, args.log))
    overhead = args.runner_overhead_seconds
    if overhead is None and args.repo and args.token and args.run_id:
        overhead = measure_runner_overhead(args.repo, args.token, args.run_id,
                                           args.run_attempt, rows)
        if overhead is not None:
            print(f"runner overhead: {overhead:.1f}s per worker job outside its process, "
                  f"the median across the run", file=sys.stderr)
    measurement = measure_run(rows, runner_overhead_seconds=overhead)

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

    blocks = _measure_blocks(rows, seen)
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

    def mutate_blocks(document):
        """Append this run's per-block seconds and bytes to whatever the branch already held.

        Args:
            document: The block document as read.

        Returns:
            The document to write back.
        """
        if not document:
            document = block_state.empty_document(blocks.source_key, blocks.profiles)
        print(block_state.record_blocks(document, blocks, build=build), file=sys.stderr)
        return document

    if args.dry_run or not (args.repo and args.token):
        document = mutate(axis_state.empty_document())
        if blocks:
            mutate_blocks({})
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
    if blocks:
        block_path = block_state.block_state_path(args.block_state_dir, blocks.source_key,
                                                  blocks.profiles)
        update_json(args.repo, args.token, args.state_branch, block_path, mutate_blocks,
                    message=f"record {len(blocks.seconds)} tile blocks from {seen} workers",
                    compact=True)
        print(f"pushed to {args.repo}@{args.state_branch}:{block_path}", file=sys.stderr)
    if args.out:
        _write_out(args.out, document)
    return 0


def _measure_blocks(rows, workers):
    """The run's per-block seconds and bytes, where every worker reported them.

    Args:
        rows: Parsed usage rows for the whole run.
        workers: How many workers reported at all.

    Returns:
        The BlockMeasurement, or None where it must not be recorded: no worker
        reported blocks, only some did, or the rows mix two runs. Each of those
        is a warning rather than a failure, costing the block history and
        nothing else.
    """
    try:
        blocks = block_state.measure_blocks(rows)
    except ValueError as error:
        print(f"::warning title=merge-axes::{error}; block seconds not recorded", file=sys.stderr)
        return None
    if blocks is None:
        print("no worker reported per-block seconds; block history not recorded",
              file=sys.stderr)
        return None
    if blocks.workers != workers:
        print(f"::warning title=merge-axes::{blocks.workers} of {workers} workers reported "
              f"per-block seconds; a partial set is a biased sample, so the block history "
              f"is not recorded", file=sys.stderr)
        return None
    return blocks


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
