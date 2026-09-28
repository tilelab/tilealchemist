"""Choosing worker_count against the run's hard limits; see docs/ARCHITECTURE.md "Sizing"."""
from collections import namedtuple

from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.fetch_batching import peak_batch_bytes
from tilealchemist.partition import count_output_tiles, partition_into_worker_blocks

# GitHub queues past ~20 concurrently running jobs on a public repo, so a run goes in waves.
DEFAULT_CONCURRENCY = 20

MATRIX_CELL_LIMIT = 256
DEFAULT_JOB_SECONDS = 6 * 3600

# The model under-predicts the slow tail by 2-4x, so a time budget is spent at this rate.
TAIL_SAFETY_FACTOR = 4.0

Limits = namedtuple("Limits", "job_seconds concurrency tail_factor")

BlockLoad = namedtuple("BlockLoad", "seconds tiles records batch_bytes")

DEFAULT_LIMITS = Limits(job_seconds=DEFAULT_JOB_SECONDS, concurrency=DEFAULT_CONCURRENCY,
                        tail_factor=TAIL_SAFETY_FACTOR)


def block_load(block, axis, profile_costs=None, setup_seconds=WORKER_SETUP_SECONDS):
    """Predict what one worker's block will cost it.

    Args:
        block: The entries assigned to that worker.
        axis: The per-axis seconds to charge.
        profile_costs: What each profile costs, as settled for this run. None
            costs tilealchemist's own work alone.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        The block's predicted seconds, output tiles, record count and peak
        batch size, as a BlockLoad.
    """
    return BlockLoad(
        seconds=setup_seconds + cost_weights(block, axis, profile_costs)[1],
        tiles=count_output_tiles(block),
        records=len(block),
        batch_bytes=peak_batch_bytes(block))


def worst_load(blocks, axis, profile_costs=None, setup_seconds=WORKER_SETUP_SECONDS):
    """Take the worst value of each axis across every block.

    No single worker need be the worst on every axis, so the result is the
    envelope a limit has to hold against rather than any one worker's load.

    Args:
        blocks: One entry block per worker.
        axis: The per-axis seconds to charge.
        profile_costs: What each profile costs, as settled for this run.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        A BlockLoad whose every field is the maximum across the blocks.
    """
    loads = [block_load(block, axis, profile_costs, setup_seconds) for block in blocks]
    return BlockLoad(*(max(getattr(load, field) for load in loads)
                       for field in BlockLoad._fields))


def breaches(load, limits):
    """Which limits this worker would break; empty means every one of them holds."""
    broken = []
    if load.seconds * limits.tail_factor > limits.job_seconds:
        broken.append("time")
    return broken


def candidate_worker_counts(limits, cell_limit=MATRIX_CELL_LIMIT):
    """The worker counts worth trying, smallest first.

    Starts at the concurrency limit, because a run goes in waves of that many
    and a count part-way into a wave costs what the whole wave costs, and
    doubles from there: every candidate stays a multiple of the concurrency,
    and a run that needs many workers reaches them in a handful of partitions
    rather than one per wave. The cell limit is the last candidate whether or
    not the doubling lands on it.

    Args:
        limits: The run's hard limits, read here for its concurrency.
        cell_limit: The most matrix cells a run may have.

    Returns:
        The worker counts to try, in increasing order.
    """
    counts, count = [], max(limits.concurrency, 1)
    while count < cell_limit:
        counts.append(count)
        count *= 2
    return counts + [cell_limit]


def choose_worker_count(entries, gaps, axis, limits=DEFAULT_LIMITS,
                        cell_limit=MATRIX_CELL_LIMIT, profile_costs=None,
                        setup_seconds=WORKER_SETUP_SECONDS):
    """Pick the first worker count whose worst worker stays inside the limits.

    Partitions at the concurrency limit and doubles until every limit holds,
    so a run that fits the first try pays one partition pass and the worst
    case pays a handful. Every limit falls as the count rises, so the first
    count that fits is also the cheapest one that does.

    Args:
        entries: The archive entries this run will walk.
        gaps: The gap records covering tiles the archive does not hold.
        axis: The per-axis seconds to charge.
        limits: The run's hard limits.
        cell_limit: The most matrix cells a run may have.
        profile_costs: What each profile costs, as settled for this run.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        The chosen count, its blocks, its worst load, and every
        `(worker_count, load, breaches)` attempt made along the way. Where no
        count fits, the largest is returned with the limits it still breaks.
    """
    attempts = []
    for worker_count in candidate_worker_counts(limits, cell_limit):
        blocks = partition_into_worker_blocks(entries, gaps, worker_count, axis)
        load = worst_load(blocks, axis, profile_costs, setup_seconds)
        broken = breaches(load, limits)
        attempts.append((worker_count, load, broken))
        if not broken:
            break
    # Whether the loop broke out or ran dry, the last partition is the one to keep.
    return worker_count, blocks, load, attempts
