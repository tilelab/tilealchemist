"""Choosing worker_count against the run's hard limits; see docs/ARCHITECTURE.md "Sizing"."""
from collections import namedtuple

from tilealchemist.cost import DEFAULT_COST_MODEL, WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.fetch_batching import peak_batch_bytes
from tilealchemist.partition import count_output_tiles, partition_into_worker_blocks

# GitHub queues past ~20 concurrently running jobs on a public repo, so a run goes in waves.
DEFAULT_CONCURRENCY = 20

MATRIX_CELL_LIMIT = 256
DEFAULT_JOB_SECONDS = 6 * 3600

# A budget is spent at this rate, for the tail the model cannot see; see "Sizing a run".
TAIL_SAFETY_FACTOR = 4.0

Limits = namedtuple("Limits", "job_seconds concurrency tail_factor")

BlockLoad = namedtuple("BlockLoad", "seconds tiles records batch_bytes")

DEFAULT_LIMITS = Limits(job_seconds=DEFAULT_JOB_SECONDS, concurrency=DEFAULT_CONCURRENCY,
                        tail_factor=TAIL_SAFETY_FACTOR)


def block_load(block, model=DEFAULT_COST_MODEL, setup_seconds=WORKER_SETUP_SECONDS):
    """Predict what one worker's block will cost it.

    Args:
        block: The entries assigned to that worker.
        model: The CostModel to price with.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        The block's predicted seconds, output tiles, record count and peak
        batch size, as a BlockLoad.
    """
    return BlockLoad(
        seconds=setup_seconds + cost_weights(block, model)[1],
        tiles=count_output_tiles(block),
        records=len(block),
        batch_bytes=peak_batch_bytes(block))


def block_loads(blocks, model=DEFAULT_COST_MODEL, setup_seconds=WORKER_SETUP_SECONDS):
    """Predict what every worker's block will cost it, in worker order.

    A caller that reports per-worker predictions and then judges the run as a
    whole wants both from one pass: pricing a planet run's blocks twice is
    work enough to notice, and two passes can only ever agree by accident.

    Args:
        blocks: One entry block per worker, in worker order.
        model: The CostModel to price with.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        One BlockLoad per block, in the same order.
    """
    return [block_load(block, model, setup_seconds) for block in blocks]


def worst_of(loads):
    """Take the worst value of each axis across loads already predicted.

    No single worker need be the worst on every axis, so the result is the
    envelope a limit has to hold against rather than any one worker's load.

    Args:
        loads: One BlockLoad per worker.

    Returns:
        A BlockLoad whose every field is the maximum across them.
    """
    return BlockLoad(*(max(getattr(load, field) for load in loads)
                       for field in BlockLoad._fields))


def worst_load(blocks, model=DEFAULT_COST_MODEL, setup_seconds=WORKER_SETUP_SECONDS):
    """Take the worst value of each axis across every block.

    Args:
        blocks: One entry block per worker.
        model: The CostModel to price with.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        A BlockLoad whose every field is the maximum across the blocks.
    """
    return worst_of(block_loads(blocks, model, setup_seconds))


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


def choose_worker_count(entries, gaps, model=DEFAULT_COST_MODEL, limits=DEFAULT_LIMITS,
                        cell_limit=MATRIX_CELL_LIMIT,
                        setup_seconds=WORKER_SETUP_SECONDS):
    """Pick the first worker count whose worst worker stays inside the limits.

    Partitions at the concurrency limit and doubles until every limit holds,
    so a run that fits the first try pays one partition pass and the worst
    case pays a handful. Every limit falls as the count rises, so the first
    count that fits is also the cheapest one that does.

    Args:
        entries: The archive entries this run will walk.
        gaps: The gap records covering tiles the archive does not hold.
        model: The CostModel to price with, which is also what the blocks are
            partitioned by, so that a run is judged by the model it was split
            with.
        limits: The run's hard limits.
        cell_limit: The most matrix cells a run may have.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        The chosen count, its blocks, its worst load, and every
        `(worker_count, load, breaches)` attempt made along the way. Where no
        count fits, the largest is returned with the limits it still breaks.
    """
    attempts = []
    for worker_count in candidate_worker_counts(limits, cell_limit):
        blocks = partition_into_worker_blocks(entries, gaps, worker_count, model)
        load = worst_load(blocks, model, setup_seconds)
        broken = breaches(load, limits)
        attempts.append((worker_count, load, broken))
        if not broken:
            break
    # Whether the loop broke out or ran dry, the last partition is the one to keep.
    return worker_count, blocks, load, attempts
