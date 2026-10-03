"""What one manifest record costs a worker, in seconds; see docs/ARCHITECTURE.md "Parallelism"."""
from collections import namedtuple

from tilealchemist.tile_blocks import tile_block

# The 39s floor across a planet run's 128 workers: runner boot, artifact download, pip install.
WORKER_SETUP_SECONDS = 39.0

AxisSeconds = namedtuple(
    "AxisSeconds", "manifest_record decode_call fetched_byte decoded_byte written_byte")

# What one profile costs a run, as the caller that assembled it settled it.
ProfileCost = namedtuple(
    "ProfileCost", "name seconds_per_tile bytes_per_output_tile gap_bytes written_share")

# Every per-byte axis charges length itself; docs/ARCHITECTURE.md has the retired decode exponent.
AXIS_SECONDS = AxisSeconds(
    manifest_record=1e-6,
    decode_call=2.5e-4,
    fetched_byte=3.3e-7,
    decoded_byte=1.1e-7,
    written_byte=1.96e-9,
)

# What one output tile weighs where no profile says; the 4.9e-7s per tile this replaces.
DEFAULT_BYTES_PER_OUTPUT_TILE = 250.0

# Pessimistic default: an undeclared profile is costed as passing tiles through.
DEFAULT_SECONDS_PER_TILE = 1e-3

# Pessimistic default again: every tile a profile is handed comes back with bytes in it.
DEFAULT_WRITTEN_SHARE = 1.0

# Seconds of in-pool work one second of a worker's wall clock buys; 1.0 is a worker with no pool.
DEFAULT_TRANSFORM_PARALLELISM = 1.0

# Everything a prediction is made from, so that no two callers can price a run differently.
CostModel = namedtuple(
    "CostModel", "axis profiles transform_parallelism block_seconds block_bytes")


def cost_model(axis=AXIS_SECONDS, profiles=None, transform_parallelism=None,
               block_seconds=None, block_bytes=None):
    """Assemble what a run is priced by.

    These travel together because splitting them is how they drift apart:
    `partition_by_cost()` once balanced on the axes alone while `worst_load()`
    sized on the axes *and* the profiles, so a run was split by one model and
    judged by another.

    Args:
        axis: The per-axis seconds to charge.
        profiles: The run's settled ProfileCosts, or None where a caller
            prices tilealchemist's own work alone.
        transform_parallelism: Seconds of in-pool work one second of a
            worker's wall clock buys, or None for a worker with no pool.
        block_seconds: The pooled profile seconds measured per home tile
            block for this run's archive and profile set, or None where
            nothing was measured. A measured block's figure replaces the
            profiles' per-tile seconds for every record homed in it.
        block_bytes: The output bytes measured per home tile block, the same
            way. A measured block's figure replaces the profiles' declared
            weight and written share for every record homed in it.

    Returns:
        The CostModel to price with.
    """
    return CostModel(axis=axis, profiles=profiles,
                     transform_parallelism=(DEFAULT_TRANSFORM_PARALLELISM
                                            if transform_parallelism is None
                                            else transform_parallelism),
                     block_seconds=block_seconds or {},
                     block_bytes=block_bytes or {})


DEFAULT_COST_MODEL = cost_model()


def _record_costs(model, records):
    """Price each record, charging a shared fetch only to the first to use it.

    Consecutive records naming the same (offset, length) are one fetch and one
    decode between them, so only the first of such a run carries that cost.

    A gap record is priced as the write it is and nothing else. It has no
    source bytes to fetch or decode, and `transform_gap()` answers every gap
    tile in the run with one call, so charging a gap a decode or a profile's
    per-tile seconds bills it for work no worker does.

    The decode and the profiles' seconds are divided by the model's transform
    parallelism, and the fetch and the write are not. Both halves are measured
    the way a worker spends them: `length_hist` and a profile's
    `transform_seconds` are summed across the pool processes that ran them,
    while `fetch_batch_blob()` and `ShardWriter.write()` run in the worker
    itself, one batch at a time.

    Where the model carries a measurement for a record's home tile block --
    the block of the record that decodes its blob, as `home_blocks()` defines
    it -- the profiles' seconds and the write are that measurement instead,
    each charged once on the block's first decoding record and nothing on the
    rest: the block is what was measured, and a partition that keeps blocks
    whole never needs it spread any finer. Records arrive in offset order,
    where one block's records are interleaved with others', so "first" means
    the first seen, not the first in a run.

    Args:
        model: The CostModel to price with.
        records: Manifest records, in the order a worker will walk them.

    Yields:
        The predicted seconds for each record, in the same order.
    """
    axis = model.axis
    pooled_per_tile = profile_seconds(model.profiles) / model.transform_parallelism
    write_seconds = axis.written_byte * written_bytes_per_tile(model.profiles)
    gap_write_seconds = axis.written_byte * gap_bytes_per_tile(model.profiles)
    measured_seconds, measured_bytes = model.block_seconds, model.block_bytes
    charged_blocks = set()
    previous_key = home = None
    for record in records:
        if not record.length:
            yield axis.manifest_record + gap_write_seconds * record.run_length
            continue
        entry = 0.0
        key = (record.offset, record.length)
        if key != previous_key:
            # Inlined home_blocks(): this is the hot loop of every partition pass.
            home = tile_block(record.tile_id)
            if home not in charged_blocks:
                charged_blocks.add(home)
                entry += (measured_seconds.get(home, 0.0) / model.transform_parallelism
                          + axis.written_byte * measured_bytes.get(home, 0.0))
            entry += (axis.fetched_byte * record.length
                      + (axis.decode_call + axis.decoded_byte * record.length)
                      / model.transform_parallelism
                      + (0.0 if home in measured_seconds else pooled_per_tile))
            previous_key = key
        write = 0.0 if home in measured_bytes else write_seconds * record.run_length
        yield axis.manifest_record + entry + write


def profile_seconds(profile_costs):
    """What one deduped tile costs across every profile in the run.

    Args:
        profile_costs: The run's settled ProfileCosts, or None where a caller
            prices tilealchemist's own work alone.

    Returns:
        The summed seconds, zero when no profiles are given.
    """
    return sum(cost.seconds_per_tile for cost in profile_costs) if profile_costs else 0.0


def written_bytes_per_tile(profile_costs):
    """What one real record tile costs to write across every profile in the run.

    A profile is handed every tile in the run and writes only some of them:
    `transform_tile()` returns None wherever there is nothing to say, and
    `_run_counts()` skips those rather than storing an empty payload. So the
    charge is the profile's weight for a tile it *does* write, times the share
    of tiles it writes at all -- `bytes_per_output_tile` is measured over the
    written tiles alone, and multiplying it by every tile in a record would
    bill a whole ocean for the coastline it does not contain.

    Args:
        profile_costs: The run's settled ProfileCosts, or None where a caller
            prices tilealchemist's own work alone.

    Returns:
        The summed bytes one record tile is expected to write, falling back to
        the default weight for one tile where no profile says.
    """
    if not profile_costs:
        return DEFAULT_BYTES_PER_OUTPUT_TILE
    return sum(cost.bytes_per_output_tile * cost.written_share for cost in profile_costs)


def gap_bytes_per_tile(profile_costs):
    """What one gap tile's output weighs across every profile in the run.

    This one is measured rather than fitted. A gap carries no source data, so
    `Profile.gap_bytes()` answers every gap tile in the run identically, and
    the caller assembling these costs asks each profile once at plan time.
    There is nothing statistical left to estimate, which also keeps gap tiles
    out of the fitted `bytes_per_output_tile` -- their size and their share of
    a run both swing far too wide to average together with real tiles. No
    written share applies either: a profile that leaves gaps out answers zero
    bytes, and one that fills them writes every one of them.

    Args:
        profile_costs: The run's settled ProfileCosts, or None.

    Returns:
        The summed bytes one gap tile comes to.
    """
    if not profile_costs:
        return DEFAULT_BYTES_PER_OUTPUT_TILE
    return sum(cost.gap_bytes for cost in profile_costs)


def cost_weights(records, model=DEFAULT_COST_MODEL):
    """Price every record, and the run as a whole.

    Args:
        records: Manifest records, in the order a worker will walk them.
            Iterated twice, so a one-shot iterator will not do.
        model: The CostModel to price with.

    Returns:
        A pair of the per-record seconds, lazily, and the total seconds the
        run is predicted to take.
    """
    return _record_costs(model, records), sum(_record_costs(model, records))
