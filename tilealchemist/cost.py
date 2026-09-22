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
    """Every record's predicted seconds, and the seconds the whole run is predicted to take."""
    return _record_costs(records, axis), sum(_record_costs(records, axis))
