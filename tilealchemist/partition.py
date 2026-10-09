"""One run's manifest entries -> one shard per worker."""
from array import array
from collections import namedtuple

from tilealchemist.cost import DEFAULT_COST_MODEL, cost_weights
from tilealchemist.tile_blocks import home_blocks

# Every home tile block in a run, what each is predicted to cost, and each
# entry's block.
TileBlockGroups = namedtuple("TileBlockGroups", "blocks seconds of_entry")


def count_output_tiles(entries):
    """Count the output tiles these entries make a worker write.

    Args:
        entries: The entries to count, real and gap entries alike.

    Returns:
        Their `run_length` sum. Counted by tile id rather than by entry
        because that is what a part holds: a deduped tile run writes every id
        it covers its own row, and so does a gap.
    """
    return sum(entry.run_length for entry in entries)


def count_gap_tiles(entries):
    """Count the output tiles these entries write from gaps, not the archive.

    Args:
        entries: The entries to count, real and gap entries alike.

    Returns:
        The `run_length` sum of the gap entries among them.
    """
    return sum(entry.run_length for entry in entries if entry.length == 0)


def _share_end_weight(total_weight, share_index, share_count):
    """The cumulative weight at which one share of the run ends.

    Args:
        total_weight: The run's whole predicted cost.
        share_index: The share that ends here, counting from zero.
        share_count: How many shares the run is split into.

    Returns:
        The cumulative weight marking the end of that share.
    """
    return total_weight * (share_index + 1) / share_count


def partition_by_cost(entries, share_count, model=DEFAULT_COST_MODEL):
    """Spread entries into shares that each carry the same predicted cost.

    Every entry is its own unit, so a share ends within one entry of its
    target. Entries sharing an offset come from one fetch, and a boundary
    through such a tile run has the next share fetch and transform that one
    tile again; keeping the tile run whole instead handed worker 0 of a planet
    run a whole share of open ocean on top of the 1081s it already held, 63m
    against an even 45m.

    Args:
        entries: The entries to spread, in walk order.
        share_count: How many shares to produce: a run's shards, or a batch's
            chunks.
        model: The CostModel to price with. It must be the same one the run is
            sized by: balancing on the axes alone while sizing on the axes and
            the profiles together handed six workers of one planet run 14.9M
            output tiles apiece that every profile then declined to write, so
            they finished in 1.5s against 762s predicted.

    Returns:
        One list of entries per share, in order. The last share takes
        whatever is left.
    """
    weights, total_weight = cost_weights(entries, model)
    shares = [[] for _ in range(share_count)]
    share_index = 0
    assigned_weight = 0.0
    for entry, weight in zip(entries, weights):
        shares[share_index].append(entry)
        assigned_weight += weight
        # An entry heavier than a share would span several, and every one it
        # covered must be skipped.
        while (share_index < share_count - 1
               and assigned_weight >= _share_end_weight(
                   total_weight, share_index, share_count)):
            share_index += 1
    return shares


def tile_block_groups(entries, model=DEFAULT_COST_MODEL):
    """Group entries by home tile block, each block priced as a whole.

    An entry goes with the block of the entry that decodes its blob, not its
    own (see `home_blocks()`), so that a deduplicated tile travels with the
    bytes it points at instead of pulling them into another worker's fetch.
    The entries arrive in offset order, which is what a worker fetches by,
    and the groups are numbered in that order too, each where its first byte
    falls -- the order `partition_by_tile_block()` hands them out in. One
    block's entries can still be interleaved with other blocks', so a block is
    not a run of entries but a set of them, kept as one group index per
    entry.

    Done once per run rather than once per worker count tried: grouping is a
    pass over every entry and the tile-block arithmetic on each, and both are
    the same for every count.

    Args:
        entries: The real entries to group, in offset order.
        model: The CostModel to price with.

    Returns:
        A TileBlockGroups: each group's block key and predicted seconds, in
        byte offset order of their first entries, and the group index of
        every entry.
    """
    group_of_block, blocks, seconds = {}, [], []
    of_entry = array("I")
    weights, unused_total = cost_weights(entries, model)
    for block, weight in zip(home_blocks(entries), weights):
        group = group_of_block.get(block)
        if group is None:
            group = group_of_block[block] = len(blocks)
            blocks.append(block)
            seconds.append(0.0)
        seconds[group] += weight
        of_entry.append(group)
    return TileBlockGroups(blocks=blocks, seconds=seconds, of_entry=of_entry)


def partition_by_tile_block(entries, groups, shard_count):
    """Fill each shard with whole tile blocks until the next one does not fit.

    Blocks are handed out in byte offset order -- the order `groups` already
    holds them in, each where its first entry falls -- so a shard holds one
    stretch of the archive's bytes and its worker fetches it in one range
    request, whatever order the archive wrote its tiles in. Tile id order only
    came to the same thing for an archive clustered by tile id. A shard ends
    at a fixed point of the run's cumulative cost,
    `total * (i + 1) / shard_count`, rather than at a per-shard budget, so
    that what one shard leaves short is the next one's to take instead of
    piling up on the last. A block bigger than a whole share still goes to a
    shard of its own rather than being split.

    Args:
        entries: The entries the groups index into, in offset order.
        groups: Their TileBlockGroups, from `tile_block_groups()`.
        shard_count: How many shards to produce.

    Returns:
        One list of entries per shard, in worker order, each still in offset
        order. The last shard takes whatever is left.
    """
    total_weight = sum(groups.seconds)
    shard_of_group = [0] * len(groups.blocks)
    shard_index, held, assigned_weight = 0, 0, 0.0
    for group, weight in enumerate(groups.seconds):
        if (shard_index < shard_count - 1 and held
                and assigned_weight + weight
                > _share_end_weight(total_weight, shard_index, shard_count)):
            shard_index, held = shard_index + 1, 0
        shard_of_group[group] = shard_index
        held += 1
        assigned_weight += weight
    shards = [[] for _ in range(shard_count)]
    for entry, group in zip(entries, groups.of_entry):
        shards[shard_of_group[group]].append(entry)
    return shards


def partition_into_shards(entries, gaps, shard_count,
                          model=DEFAULT_COST_MODEL, groups=None):
    """Build each worker's shard from both the real entries and the gaps.

    The two are spread separately, so that gap work, which needs no fetch at
    all, lands evenly instead of following the fetches around. The real
    entries go by whole tile blocks, the unit their profile seconds are
    measured in; the gaps by entry, having no profile seconds to measure.

    Args:
        entries: The archive's directory entries for this run.
        gaps: The gap entries covering what the archive does not hold.
        shard_count: How many shards to produce, one per worker.
        model: The CostModel to price with.
        groups: The entries' TileBlockGroups, where the caller already
            grouped them under the same model; grouped here otherwise.

    Returns:
        One list of entries per shard, its real entries before its gaps.
    """
    if groups is None:
        groups = tile_block_groups(entries, model)
    gap_shares = partition_by_cost(gaps, shard_count, model=model)
    real_shards = partition_by_tile_block(entries, groups, shard_count)
    return [real_shard + gap_share
            for real_shard, gap_share in zip(real_shards, gap_shares)]
