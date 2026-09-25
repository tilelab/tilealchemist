"""Choosing worker_count against the run's hard limits; see docs/ARCHITECTURE.md "Sizing"."""
from collections import namedtuple

from tilealchemist.budgets import peak_batch_bytes
from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.partition import partition_into_worker_blocks

# GitHub queues past ~20 concurrently running jobs on a public repo, so a run goes in waves.
DEFAULT_CONCURRENCY = 20

MATRIX_CELL_LIMIT = 256
DEFAULT_JOB_SECONDS = 6 * 3600
DEFAULT_WORKER_RAM_BYTES = 14 * 1024 ** 3
DEFAULT_WORKER_DISK_BYTES = 10 * 1024 ** 3

# The model under-predicts the slow tail by 2-4x, so a time budget is spent at this rate.
TAIL_SAFETY_FACTOR = 4.0

Limits = namedtuple("Limits", "job_seconds ram_bytes disk_bytes concurrency tail_factor")

BlockLoad = namedtuple("BlockLoad", "seconds rss_bytes disk_bytes records batch_bytes")

DEFAULT_LIMITS = Limits(job_seconds=DEFAULT_JOB_SECONDS, ram_bytes=DEFAULT_WORKER_RAM_BYTES,
                        disk_bytes=DEFAULT_WORKER_DISK_BYTES, concurrency=DEFAULT_CONCURRENCY,
                        tail_factor=TAIL_SAFETY_FACTOR)


def block_load(block, runner, axis, max_fetch_gap):
    """Predict what one worker's block will cost it.

    Args:
        block: The entries assigned to that worker.
        runner: The calibrated runner profile the RAM and disk rates come from.
        axis: The per-axis seconds to charge.
        max_fetch_gap: The largest gap between two entries that still shares
            one ranged fetch.

    Returns:
        The block's predicted seconds, peak RSS, output bytes, record count
        and peak batch size, as a BlockLoad.
    """
    batch_bytes = peak_batch_bytes(block, max_fetch_gap)
    return BlockLoad(
        seconds=WORKER_SETUP_SECONDS + cost_weights(block, axis)[1],
        rss_bytes=runner.rss_base + runner.rss_per_batch_byte * batch_bytes,
        disk_bytes=sum(record.run_length for record in block) * runner.bytes_per_output_tile,
        records=len(block),
        batch_bytes=batch_bytes)


def worst_load(blocks, runner, axis, max_fetch_gap):
    """Take the worst value of each axis across every block.

    No single worker need be the worst on every axis, so the result is the
    envelope a limit has to hold against rather than any one worker's load.

    Args:
        blocks: One entry block per worker.
        runner: The calibrated runner profile.
        axis: The per-axis seconds to charge.
        max_fetch_gap: The largest gap that still shares one ranged fetch.

    Returns:
        A BlockLoad whose every field is the maximum across the blocks.
    """
    loads = [block_load(block, runner, axis, max_fetch_gap) for block in blocks]
    return BlockLoad(*(max(getattr(load, field) for load in loads)
                       for field in BlockLoad._fields))


def breaches(load, limits):
    """Which budgets this worker would break; empty means every one of them holds."""
    broken = []
    if load.seconds * limits.tail_factor > limits.job_seconds:
        broken.append("time")
    if load.rss_bytes > limits.ram_bytes:
        broken.append("ram")
    if load.disk_bytes > limits.disk_bytes:
        broken.append("disk")
    return broken


def candidate_counts(limits, cell_limit=MATRIX_CELL_LIMIT):
    """The worker counts worth trying, smallest first.

    Stepped by the concurrency limit, because a run goes in waves of that many
    and a count part-way into a wave costs what the whole wave costs.

    Args:
        limits: The run's hard limits, read here for its concurrency.
        cell_limit: The most matrix cells a run may have.

    Returns:
        The worker counts to try, in increasing order.
    """
    step = max(limits.concurrency, 1)
    counts = list(range(step, cell_limit + 1, step))
    return counts or [cell_limit]


def choose_worker_count(entries, gaps, runner, axis, caps, limits=DEFAULT_LIMITS,
                        max_fetch_gap=None, cell_limit=MATRIX_CELL_LIMIT):
    """Pick the smallest worker count whose worst worker stays inside the limits.

    Args:
        entries: The archive entries this run will walk.
        gaps: The gap records covering tiles the archive does not hold.
        runner: The calibrated runner profile.
        axis: The per-axis seconds to charge.
        caps: The per-block caps a partition has to respect.
        limits: The run's hard limits.
        max_fetch_gap: The largest gap that still shares one ranged fetch, or
            None to take it from `caps`.
        cell_limit: The most matrix cells a run may have.

    Returns:
        The chosen count, its blocks, its worst load, and every
        `(worker_count, load, breaches)` attempt made along the way. Where no
        count fits, the largest is returned with the budgets it still breaks.
    """
    gap = caps.max_fetch_gap if max_fetch_gap is None else max_fetch_gap
    attempts = []
    for worker_count in candidate_counts(limits, cell_limit):
        blocks = partition_into_worker_blocks(entries, gaps, worker_count, caps, axis)
        load = worst_load(blocks, runner, axis, gap)
        broken = breaches(load, limits)
        attempts.append((worker_count, load, broken))
        if not broken:
            return worker_count, blocks, load, attempts
    worker_count, load, _broken = attempts[-1]
    return worker_count, partition_into_worker_blocks(
        entries, gaps, worker_count, caps, axis), load, attempts
