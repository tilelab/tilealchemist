"""What one manifest record costs a worker, in seconds; see docs/ARCHITECTURE.md "Parallelism"."""
from collections import namedtuple

# The 39s floor across a planet run's 128 workers: runner boot, artifact download, pip install.
WORKER_SETUP_SECONDS = 39.0

AxisSeconds = namedtuple(
    "AxisSeconds", "manifest_record decode_call fetched_byte decoded_byte written_byte")

# What one profile costs a run, as the caller that assembled it settled it.
ProfileCost = namedtuple("ProfileCost", "name seconds_per_tile bytes_per_output_tile gap_bytes")

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


def _record_costs(records, axis, per_tile_seconds=0.0,
                   per_tile_bytes=DEFAULT_BYTES_PER_OUTPUT_TILE,
                   per_gap_tile_bytes=DEFAULT_BYTES_PER_OUTPUT_TILE):
    """Price each record, charging a shared fetch only to the first to use it.

    Consecutive records naming the same (offset, length) are one fetch and one
    decode between them, so only the first of such a run carries that cost.

    A gap record is priced as the write it is and nothing else. It has no
    source bytes to fetch or decode, and `transform_gap()` answers every gap
    tile in the run with one call, so charging a gap a decode or a profile's
    per-tile seconds bills it for work no worker does.

    Args:
        records: Manifest records, in the order a worker will walk them.
        axis: The per-axis seconds to charge.
        per_tile_seconds: What every profile together costs on one deduped
            tile, charged where the decode is: `_entry_outputs()` runs each
            profile once per distinct entry, not once per record.
        per_tile_bytes: What every profile's output for one tile weighs
            together. The write is charged on bytes, not on tiles: a tile is
            only as expensive to store as it is large, and how large that is
            belongs to the profile that shaped it.
        per_gap_tile_bytes: The same for a gap tile, which is a different and
            exactly known size; see `gap_output_bytes()`.

    Yields:
        The predicted seconds for each record, in the same order.
    """
    previous_key = None
    write_seconds = axis.written_byte * per_tile_bytes
    gap_write_seconds = axis.written_byte * per_gap_tile_bytes
    for record in records:
        if not record.length:
            yield axis.manifest_record + gap_write_seconds * record.run_length
            continue
        key = (record.offset, record.length)
        entry = 0.0
        if key != previous_key:
            entry = (axis.decode_call + axis.fetched_byte * record.length
                     + axis.decoded_byte * record.length
                     + per_tile_seconds)
            previous_key = key
        yield axis.manifest_record + entry + write_seconds * record.run_length


def profile_seconds(profile_costs):
    """What one deduped tile costs across every profile in the run.

    Args:
        profile_costs: The run's settled ProfileCosts, or None where a caller
            prices tilealchemist's own work alone.

    Returns:
        The summed seconds, zero when no profiles are given.
    """
    return sum(cost.seconds_per_tile for cost in profile_costs) if profile_costs else 0.0


def output_bytes(profile_costs):
    """What one real tile's output weighs across every profile in the run.

    Args:
        profile_costs: The run's settled ProfileCosts, or None where a caller
            prices tilealchemist's own work alone.

    Returns:
        The summed bytes per output tile, falling back to the default weight
        for one tile where no profile says.
    """
    if not profile_costs:
        return DEFAULT_BYTES_PER_OUTPUT_TILE
    return sum(cost.bytes_per_output_tile for cost in profile_costs)


def gap_output_bytes(profile_costs):
    """What one gap tile's output weighs across every profile in the run.

    This one is measured rather than fitted. A gap carries no source data, so
    `Profile.gap_bytes()` answers every gap tile in the run identically, and
    the caller assembling these costs asks each profile once at plan time.
    There is nothing statistical left to estimate, which also keeps gap tiles
    out of the fitted `bytes_per_output_tile` -- their size and their share of
    a run both swing far too wide to average together with real tiles.

    Args:
        profile_costs: The run's settled ProfileCosts, or None.

    Returns:
        The summed bytes one gap tile comes to.
    """
    if not profile_costs:
        return DEFAULT_BYTES_PER_OUTPUT_TILE
    return sum(cost.gap_bytes for cost in profile_costs)


def cost_weights(records, axis=AXIS_SECONDS, profile_costs=None):
    """Price every record, and the run as a whole.

    Args:
        records: Manifest records, in the order a worker will walk them.
            Iterated twice, so a one-shot iterator will not do.
        axis: The per-axis seconds to charge.
        profile_costs: The run's settled ProfileCosts, whose per-tile seconds
            are charged once per distinct entry and whose output weights set
            the write cost. None prices the fixed axes alone.

    Returns:
        A pair of the per-record seconds, lazily, and the total seconds the
        run is predicted to take.
    """
    per_tile, per_tile_bytes = profile_seconds(profile_costs), output_bytes(profile_costs)
    gap_bytes = gap_output_bytes(profile_costs)
    return (_record_costs(records, axis, per_tile, per_tile_bytes, gap_bytes),
            sum(_record_costs(records, axis, per_tile, per_tile_bytes, gap_bytes)))
