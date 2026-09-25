"""One run's directory entries -> one block of work per worker."""
import itertools
import operator

from tilealchemist.budgets import BlockBudget
from tilealchemist.cost import AXIS_SECONDS, cost_weights
from tilealchemist.manifest import Entry
from tilealchemist.pmtiles_index import tile_id_bounds

# Caps one gap record, so a single unbroken gap cannot land wholly on one worker.
GAP_CHUNK_SIZE = 200_000


def compute_gaps(entries, min_zoom, max_zoom):
    """Find the tile ranges the archive holds nothing for.

    Args:
        entries: The archive's directory entries for this run.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.

    Returns:
        Gap records covering every tile id in range that no entry covers,
        chunked so that no one record is larger than GAP_CHUNK_SIZE.
    """
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    gaps = []
    expected = tile_id_start
    # Entries never overlap, so sorted by tile_id their ends are non-decreasing too.
    for entry in sorted(entries, key=operator.attrgetter("tile_id")):
        if entry.tile_id > expected:
            gaps.extend(_chunk_gap(expected, entry.tile_id))
        expected = entry.tile_id + entry.run_length
    if expected < tile_id_limit:
        gaps.extend(_chunk_gap(expected, tile_id_limit))
    return gaps


def _chunk_gap(start, end):
    """Cut one gap into records small enough to spread across workers.

    Args:
        start: First uncovered tile id.
        end: One past the last uncovered tile id.

    Returns:
        Gap records spanning the range, each marked by the `length=0` sentinel
        that split_manifest_entries() tells a gap by.
    """
    return [Entry(tile_id=chunk_start, offset=0, length=0,
                  run_length=min(GAP_CHUNK_SIZE, end - chunk_start))
            for chunk_start in range(start, end, GAP_CHUNK_SIZE)]


def _share_end(total_weight, worker_index, worker_count):
    """The cumulative weight at which one worker's share of the run ends.

    Args:
        total_weight: The run's whole predicted cost.
        worker_index: The worker whose share ends here, counting from zero.
        worker_count: How many workers the run is split across.

    Returns:
        The cumulative weight marking the end of that worker's share.
    """
    return total_weight * (worker_index + 1) / worker_count


def _atomic_groups(weighted_records, atomic_key, share_limit, records_limit=0):
    """Group records that must not be split across two workers.

    Records sharing an `atomic_key` come from one fetch, so splitting them
    would have two workers download the same bytes. A run is broken up anyway
    once it outgrows a worker's share or the record cap, since the alternative
    is one worker carrying it whole.

    Args:
        weighted_records: `(record, weight)` pairs, in walk order.
        atomic_key: What makes two records inseparable, or None to treat each
            record as its own group.
        share_limit: The weight one worker's share carries.
        records_limit: The most records a group may hold, or 0 for no limit.

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
            if run and (run_weight + weight > share_limit
                        or (records_limit and len(run) >= records_limit)):
                yield run, run_weight
                run, run_weight = [], 0.0
            run.append(record)
            run_weight += weight
        yield run, run_weight


def partition_by_cost(records, worker_count, atomic_key=None, caps=None, axis=AXIS_SECONDS):
    """Spread records across workers so each carries a similar predicted cost.

    Args:
        records: The records to spread, in walk order.
        worker_count: How many blocks to produce.
        atomic_key: What makes two records inseparable, or None.
        caps: The per-block caps to respect, or None for none.
        axis: The per-axis seconds to charge.

    Returns:
        One list of records per worker, in worker order. The last block takes
        whatever is left, however far past its share that puts it.
    """
    weights, total_weight = cost_weights(records, axis)
    groups = _atomic_groups(zip(records, weights), atomic_key,
                             total_weight / worker_count,
                             caps.records if caps else 0)
    blocks = [[] for _ in range(worker_count)]
    budget = BlockBudget(caps)
    worker_index = 0
    assigned_weight = 0.0
    for group, group_weight in groups:
        if worker_index < worker_count - 1 and budget.would_exceed(group):
            worker_index += 1
            budget.reset()
        blocks[worker_index].extend(group)
        budget.add(group)
        assigned_weight += group_weight
        # A group can span several shares, and every one it covered must be skipped.
        while (worker_index < worker_count - 1
               and assigned_weight >= _share_end(total_weight, worker_index, worker_count)):
            worker_index += 1
            budget.reset()
    return blocks


def partition_into_worker_blocks(entries, gaps, worker_count, caps=None, axis=AXIS_SECONDS):
    """Build each worker's block from both the real entries and the gaps.

    The two are spread separately, so that gap work, which needs no fetch at
    all, lands evenly instead of following the fetches around.

    Args:
        entries: The archive's directory entries for this run.
        gaps: The gap records covering what the archive does not hold.
        worker_count: How many blocks to produce.
        caps: The per-block caps to respect, or None for none.
        axis: The per-axis seconds to charge.

    Returns:
        One list of records per worker, its real entries before its gaps.
    """
    gap_blocks = partition_by_cost(gaps, worker_count, axis=axis)
    real_caps = _caps_less_gaps(caps, gap_blocks)
    real_blocks = partition_by_cost(entries, worker_count,
                                    atomic_key=operator.attrgetter("offset"), caps=real_caps,
                                    axis=axis)
    return [real_block + gap_block
            for real_block, gap_block in zip(real_blocks, gap_blocks)]


def _caps_less_gaps(caps, gap_blocks):
    """Leave room in the record cap for the gaps a block will also carry.

    Args:
        caps: The caps the finished block has to fit inside, or None.
        gap_blocks: The gap records already assigned to each worker.

    Returns:
        The caps to hold the real entries to, never dropping below one
        record.
    """
    if caps is None or not caps.records:
        return caps
    gap_records = max((len(block) for block in gap_blocks), default=0)
    return caps._replace(records=max(caps.records - gap_records, 1))
