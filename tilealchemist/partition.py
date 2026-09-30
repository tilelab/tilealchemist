"""One run's directory entries -> one block of work per worker."""
from tilealchemist.cost import DEFAULT_COST_MODEL, cost_weights


def count_output_tiles(entries):
    """Count the output tiles these records make a worker write.

    Args:
        entries: The records to count, real entries and gaps alike.

    Returns:
        Their `run_length` sum. Counted by tile id rather than by record
        because that is what a shard holds: a deduped run writes every id it
        covers its own row, and so does a gap.
    """
    return sum(entry.run_length for entry in entries)


def count_gap_tiles(entries):
    """Count the output tiles these records write from gaps rather than from the archive."""
    return sum(entry.run_length for entry in entries if entry.length == 0)


def _share_end_weight(total_weight, worker_index, worker_count):
    """The cumulative weight at which one worker's share of the run ends.

    Args:
        total_weight: The run's whole predicted cost.
        worker_index: The worker whose share ends here, counting from zero.
        worker_count: How many workers the run is split across.

    Returns:
        The cumulative weight marking the end of that worker's share.
    """
    return total_weight * (worker_index + 1) / worker_count


def partition_by_cost(records, worker_count, model=DEFAULT_COST_MODEL):
    """Spread records across workers so each carries the same predicted cost.

    Every record is its own unit, so a block ends within one record of its
    share. Records sharing an offset come from one fetch, and a boundary
    through such a run has the next worker fetch and transform that one tile
    again; keeping the run whole instead handed worker 0 of a planet run a
    whole share of open ocean on top of the 1081s it already held, 63m
    against an even 45m.

    Args:
        records: The records to spread, in walk order.
        worker_count: How many blocks to produce.
        model: The CostModel to price with. It must be the same one the run is
            sized by: balancing on the axes alone while sizing on the axes and
            the profiles together handed six workers of one planet run 14.9M
            output tiles apiece that every profile then declined to write, so
            they finished in 1.5s against 762s predicted.

    Returns:
        One list of records per worker, in worker order. The last block takes
        whatever is left.
    """
    weights, total_weight = cost_weights(records, model)
    blocks = [[] for _ in range(worker_count)]
    worker_index = 0
    assigned_weight = 0.0
    for record, weight in zip(records, weights):
        blocks[worker_index].append(record)
        assigned_weight += weight
        # A record heavier than a share would span several, and every one it covered must be skipped.
        while (worker_index < worker_count - 1
               and assigned_weight >= _share_end_weight(total_weight, worker_index, worker_count)):
            worker_index += 1
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count, model=DEFAULT_COST_MODEL):
    """Build each worker's block from both the real entries and the gaps.

    The two are spread separately, so that gap work, which needs no fetch at
    all, lands evenly instead of following the fetches around.

    Args:
        entries: The archive's directory entries for this run.
        gaps: The gap records covering what the archive does not hold.
        worker_count: How many blocks to produce.
        model: The CostModel to price with.

    Returns:
        One list of records per worker, its real entries before its gaps.
    """
    gap_blocks = partition_by_cost(gaps, worker_count, model=model)
    real_blocks = partition_by_cost(entries, worker_count, model=model)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]

