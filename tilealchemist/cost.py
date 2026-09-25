"""What one manifest record costs a worker, in seconds; see docs/ARCHITECTURE.md "Parallelism"."""
from collections import namedtuple

# Fitted against a planet run's 128 worker durations: decode outgrows byte length.
DENSITY_EXPONENT = 1.5

# The 39s floor across a planet run's 128 workers: runner boot, artifact download, pip install.
WORKER_SETUP_SECONDS = 39.0

AxisSeconds = namedtuple(
    "AxisSeconds", "manifest_record decode_call fetched_byte decoded_byte output_tile")

# decoded_byte is charged on length**DENSITY_EXPONENT, the other per-byte axis on length itself.
AXIS_SECONDS = AxisSeconds(
    manifest_record=1e-6,
    decode_call=2.5e-4,
    fetched_byte=3.3e-7,
    decoded_byte=2e-9,
    output_tile=4.9e-7,
)


def _record_costs(records, axis):
    """Price each record, charging a shared fetch only to the first to use it.

    Consecutive records naming the same (offset, length) are one fetch and one
    decode between them, so only the first of such a run carries that cost.

    Args:
        records: Manifest records, in the order a worker will walk them.
        axis: The per-axis seconds to charge.

    Yields:
        The predicted seconds for each record, in the same order.
    """
    previous_key = None
    for record in records:
        key = (record.offset, record.length)
        entry = 0.0
        if key != previous_key:
            entry = (axis.decode_call + axis.fetched_byte * record.length
                     + axis.decoded_byte * record.length ** DENSITY_EXPONENT)
            previous_key = key
        yield axis.manifest_record + entry + axis.output_tile * record.run_length


def cost_weights(records, axis=AXIS_SECONDS):
    """Price every record, and the run as a whole.

    Args:
        records: Manifest records, in the order a worker will walk them.
            Iterated twice, so a one-shot iterator will not do.
        axis: The per-axis seconds to charge.

    Returns:
        A pair of the per-record seconds, lazily, and the total seconds the
        run is predicted to take.
    """
    return _record_costs(records, axis), sum(_record_costs(records, axis))
