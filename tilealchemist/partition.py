"""One run's directory entries -> one block of work per worker."""
import itertools
import operator

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


def _atomic_groups(weighted_records, atomic_key, share_limit):
    """Group records that must not be split across two workers.

    Records sharing an `atomic_key` come from one fetch, so splitting them
    would have two workers download the same bytes. A run is broken up anyway
    once it outgrows a worker's share, since the alternative is one worker
    carrying it whole.

    Args:
        weighted_records: `(record, weight)` pairs, in walk order.
        atomic_key: What makes two records inseparable, or None to treat each
            record as its own group.
        share_limit: The weight one worker's share carries.

    Yields:
        `(records, weight)` per group, in the order given.
    """
    if atomic_key is None:
        for record, weight in weighted_records:
            yield [record], weight
        return
    for _key, group in itertools.groupby(weighted_records,
                                          key=lambda pair: atomic_key(pair[0])):
        run, run_weight = [], 0.0
        for record, weight in group:
            if run and run_weight + weight > share_limit:
                yield run, run_weight
                run, run_weight = [], 0.0
            run.append(record)
            run_weight += weight
        yield run, run_weight


def partition_by_cost(records, worker_count, atomic_key=None, model=DEFAULT_COST_MODEL):
    """Spread records across workers so each carries a similar predicted cost.

    Args:
        records: The records to spread, in walk order.
        worker_count: How many blocks to produce.
        atomic_key: What makes two records inseparable, or None.
        model: The CostModel to price with. It must be the same one the run is
            sized by: balancing on the axes alone while sizing on the axes and
            the profiles together handed six workers of one planet run 14.9M
            output tiles apiece that every profile then declined to write, so
            they finished in 1.5s against 762s predicted.

    Returns:
        One list of records per worker, in worker order. The last block takes
        whatever is left, however far past its share that puts it.
    """
    weights, total_weight = cost_weights(records, model)
    groups = _atomic_groups(zip(records, weights), atomic_key,
                             total_weight / worker_count)
    blocks = [[] for _ in range(worker_count)]
    worker_index = 0
    assigned_weight = 0.0
    for group, group_weight in groups:
        blocks[worker_index].extend(group)
        assigned_weight += group_weight
        # A group can span several shares, and every one it covered must be skipped.
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
    real_blocks = partition_by_cost(entries, worker_count,
                                    atomic_key=operator.attrgetter("offset"), model=model)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]

