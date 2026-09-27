"""Choosing worker_count against the run's hard limits; see docs/ARCHITECTURE.md "Sizing"."""
from collections import namedtuple

from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.fetch_batching import peak_batch_bytes
from tilealchemist.partition import block_tiles, partition_into_worker_blocks

# GitHub queues past ~20 concurrently running jobs on a public repo, so a run goes in waves.
DEFAULT_CONCURRENCY = 20

MATRIX_CELL_LIMIT = 256
DEFAULT_JOB_SECONDS = 6 * 3600

# The model under-predicts the slow tail by 2-4x, so a time budget is spent at this rate.
TAIL_SAFETY_FACTOR = 4.0

Limits = namedtuple("Limits", "job_seconds max_tiles concurrency tail_factor")

BlockLoad = namedtuple("BlockLoad", "seconds tiles records batch_bytes")

DEFAULT_LIMITS = Limits(job_seconds=DEFAULT_JOB_SECONDS, max_tiles=0,
                        concurrency=DEFAULT_CONCURRENCY, tail_factor=TAIL_SAFETY_FACTOR)


def block_load(block, axis, profiles=None):
    """Predict what one worker's block will cost it.

    Args:
        block: The entries assigned to that worker.
        axis: The per-axis seconds to charge.
        profiles: The profiles the run builds, priced per deduped tile. None
            costs tilealchemist's own work alone.

    Returns:
        The block's predicted seconds, output tiles, record count and peak
        batch size, as a BlockLoad.
    """
    return BlockLoad(
        seconds=WORKER_SETUP_SECONDS + cost_weights(block, axis, profiles)[1],
        tiles=block_tiles(block),
        records=len(block),
        batch_bytes=peak_batch_bytes(block))


def worst_load(blocks, axis, profiles=None):
    """Take the worst value of each axis across every block.

    No single worker need be the worst on every axis, so the result is the
    envelope a limit has to hold against rather than any one worker's load.

    Args:
        blocks: One entry block per worker.
        axis: The per-axis seconds to charge.
        profiles: The profiles the run builds.

    Returns:
        A BlockLoad whose every field is the maximum across the blocks.
    """
    loads = [block_load(block, axis, profiles) for block in blocks]
    return BlockLoad(*(max(getattr(load, field) for load in loads)
                       for field in BlockLoad._fields))


def breaches(load, limits):
    """Which limits this worker would break; empty means every one of them holds."""
    broken = []
    if load.seconds * limits.tail_factor > limits.job_seconds:
        broken.append("time")
    if limits.max_tiles and load.tiles > limits.max_tiles:
        broken.append("tiles")
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


def choose_worker_count(entries, gaps, axis, limits=DEFAULT_LIMITS,
                        cell_limit=MATRIX_CELL_LIMIT, profiles=None):
    """Pick the smallest worker count whose worst worker stays inside the limits.

    Args:
        entries: The archive entries this run will walk.
        gaps: The gap records covering tiles the archive does not hold.
        axis: The per-axis seconds to charge.
        limits: The run's hard limits, whose tile cap the partition also
            holds each block to.
        cell_limit: The most matrix cells a run may have.
        profiles: The profiles the run builds, priced into the time.

    Returns:
        The chosen count, its blocks, its worst load, and every
        `(worker_count, load, breaches)` attempt made along the way. Where no
        count fits, the largest is returned with the limits it still breaks.
    """
    attempts = []
    for worker_count in candidate_counts(limits, cell_limit):
        blocks = partition_into_worker_blocks(entries, gaps, worker_count,
                                               limits.max_tiles, axis)
        load = worst_load(blocks, axis, profiles)
        broken = breaches(load, limits)
        attempts.append((worker_count, load, broken))
        if not broken:
            return worker_count, blocks, load, attempts
    worker_count, load, _broken = attempts[-1]
    return worker_count, partition_into_worker_blocks(
        entries, gaps, worker_count, limits.max_tiles, axis), load, attempts
