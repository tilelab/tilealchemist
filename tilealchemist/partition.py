"""One run's directory entries -> one block of work per worker."""
from array import array
from collections import namedtuple

from tilealchemist.cost import DEFAULT_COST_MODEL, cost_weights
from tilealchemist.tile_blocks import tile_block

# Every tile block in a run, what each is predicted to cost, and which block each record is in.
TileBlockGroups = namedtuple("TileBlockGroups", "blocks seconds of_record")


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


def tile_block_groups(records, model=DEFAULT_COST_MODEL):
    """Group records by tile block, each block priced as a whole.

    The records arrive in offset order, which is what a worker fetches by,
    and in that order one block's records are interleaved with other blocks':
    a deduplicated tile points back at the blob its first copy wrote, however
    far away that copy is. So a block is not a run of records but a set of
    them, kept as one group index per record.

    Done once per run rather than once per worker count tried: grouping is a
    pass over every record and the tile-block arithmetic on each, and both are
    the same for every count.

    Args:
        records: The real entries to group, in offset order.
        model: The CostModel to price with.

    Returns:
        A TileBlockGroups: each group's block key and predicted seconds, in
        order of first appearance, and the group index of every record.
    """
    group_of_block, blocks, seconds = {}, [], []
    of_record = array("I")
    weights, _total = cost_weights(records, model)
    for record, weight in zip(records, weights):
        block = tile_block(record.tile_id)
        group = group_of_block.get(block)
        if group is None:
            group = group_of_block[block] = len(blocks)
            blocks.append(block)
            seconds.append(0.0)
        seconds[group] += weight
        of_record.append(group)
    return TileBlockGroups(blocks=blocks, seconds=seconds, of_record=of_record)


def partition_by_tile_block(records, groups, worker_count):
    """Fill each worker with whole tile blocks until the next one does not fit.

    Blocks are handed out in tile id order, so a worker holds one stretch of
    the map and, the archive being written in roughly that order, one stretch
    of its bytes. A worker's share ends at a fixed point of the run's
    cumulative cost, `total * (i + 1) / worker_count`, rather than at a
    per-worker budget, so that what one worker leaves short is the next one's
    to take instead of piling up on the last. A block bigger than a whole
    share still goes to a worker of its own rather than being split.

    Args:
        records: The records the groups index into, in offset order.
        groups: Their TileBlockGroups, from `tile_block_groups()`.
        worker_count: How many blocks to produce.

    Returns:
        One list of records per worker, in worker order, each still in offset
        order. The last block takes whatever is left.
    """
    total_weight = sum(groups.seconds)
    worker_of_group = [0] * len(groups.blocks)
    worker_index, held, assigned_weight = 0, 0, 0.0
    for group in sorted(range(len(groups.blocks)), key=groups.blocks.__getitem__):
        weight = groups.seconds[group]
        if (worker_index < worker_count - 1 and held
                and assigned_weight + weight
                > _share_end_weight(total_weight, worker_index, worker_count)):
            worker_index, held = worker_index + 1, 0
        worker_of_group[group] = worker_index
        held += 1
        assigned_weight += weight
    blocks = [[] for _ in range(worker_count)]
    for record, group in zip(records, groups.of_record):
        blocks[worker_of_group[group]].append(record)
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count, model=DEFAULT_COST_MODEL,
                                 groups=None):
    """Build each worker's block from both the real entries and the gaps.

    The two are spread separately, so that gap work, which needs no fetch at
    all, lands evenly instead of following the fetches around. The real
    entries go by whole tile blocks, the unit their profile seconds are
    measured in; the gaps by record, having no profile seconds to measure.

    Args:
        entries: The archive's directory entries for this run.
        gaps: The gap records covering what the archive does not hold.
        worker_count: How many blocks to produce.
        model: The CostModel to price with.
        groups: The entries' TileBlockGroups, where the caller already
            grouped them under the same model; grouped here otherwise.

    Returns:
        One list of records per worker, its real entries before its gaps.
    """
    if groups is None:
        groups = tile_block_groups(entries, model)
    gap_blocks = partition_by_cost(gaps, worker_count, model=model)
    real_blocks = partition_by_tile_block(entries, groups, worker_count)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]
