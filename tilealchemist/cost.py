"""What one manifest record costs a worker, in seconds; see docs/ARCHITECTURE.md "Parallelism"."""
from collections import namedtuple

# The 39s floor across a planet run's 128 workers: runner boot, artifact download, pip install.
WORKER_SETUP_SECONDS = 39.0

AxisSeconds = namedtuple(
    "AxisSeconds", "manifest_record decode_call fetched_byte decoded_byte output_tile")

# Both per-byte axes charge length itself; docs/ARCHITECTURE.md has the retired decode exponent.
AXIS_SECONDS = AxisSeconds(
    manifest_record=1e-6,
    decode_call=2.5e-4,
    fetched_byte=3.3e-7,
    decoded_byte=1.1e-7,
    output_tile=4.9e-7,
)


def _record_costs(records, axis, per_tile_seconds=0.0):
    """Price each record, charging a shared fetch only to the first to use it.

    Consecutive records naming the same (offset, length) are one fetch and one
    decode between them, so only the first of such a run carries that cost.

    Args:
        records: Manifest records, in the order a worker will walk them.
        axis: The per-axis seconds to charge.
        per_tile_seconds: What every profile together costs on one deduped
            tile, charged where the decode is: `_entry_outputs()` runs each
            profile once per distinct entry, not once per record.

    Yields:
        The predicted seconds for each record, in the same order.
    """
    previous_key = None
    for record in records:
        key = (record.offset, record.length)
        entry = 0.0
        if key != previous_key:
            entry = (axis.decode_call + axis.fetched_byte * record.length
                     + axis.decoded_byte * record.length
                     + per_tile_seconds)
            previous_key = key
        yield axis.manifest_record + entry + axis.output_tile * record.run_length


def profile_seconds(profiles):
    """What one deduped tile costs across every profile in the run.

    Args:
        profiles: The profiles the run builds, or None where a caller prices
            tilealchemist's own work alone.

    Returns:
        The summed seconds, zero when no profiles are given.
    """
    return sum(profile.seconds_per_tile for profile in profiles) if profiles else 0.0


def cost_weights(records, axis=AXIS_SECONDS, profiles=None):
    """Price every record, and the run as a whole.

    Args:
        records: Manifest records, in the order a worker will walk them.
            Iterated twice, so a one-shot iterator will not do.
        axis: The per-axis seconds to charge.
        profiles: The profiles the run builds, whose per-tile seconds are
            charged once per distinct entry. None prices the fixed axes alone.

    Returns:
        A pair of the per-record seconds, lazily, and the total seconds the
        run is predicted to take.
    """
    per_tile = profile_seconds(profiles)
    return (_record_costs(records, axis, per_tile),
            sum(_record_costs(records, axis, per_tile)))
