"""Hard per-worker limits, as limits rather than prices; see docs/ARCHITECTURE.md "Budgets"."""
from collections import namedtuple

from tilealchemist.fetch_batching import DEFAULT_MAX_FETCH_GAP

# One Entry namedtuple, measured: four fields plus the object and its pointer table.
MANIFEST_RECORD_BYTES = 120

DEFAULT_MANIFEST_RAM_BYTES = 512 * 1024 * 1024
DEFAULT_PEAK_BATCH_BYTES = 1024 * 1024 * 1024

Caps = namedtuple("Caps", "records batch_bytes max_fetch_gap")

EMPTY_BATCH_STATE = (0, None, 0)


def caps_from_budgets(manifest_ram_bytes, peak_batch_bytes,
                      max_fetch_gap=DEFAULT_MAX_FETCH_GAP):
    return Caps(records=manifest_ram_bytes // MANIFEST_RECORD_BYTES if manifest_ram_bytes else 0,
                batch_bytes=peak_batch_bytes or 0, max_fetch_gap=max_fetch_gap)


def _batch_state(state, entries, max_fetch_gap):
    peak, start, reach = state
    for entry in entries:
        if entry.length == 0:
            continue
        if start is None or entry.offset - reach > max_fetch_gap:
            start = reach = entry.offset
        reach = max(reach, entry.offset + entry.length)
        peak = max(peak, reach - start)
    return peak, start, reach


def peak_batch_bytes(entries, max_fetch_gap=DEFAULT_MAX_FETCH_GAP):
    """The largest single range request these entries will need, by plan_fetch_batches' rule."""
    return _batch_state(EMPTY_BATCH_STATE, entries, max_fetch_gap)[0]


def cap_overruns(blocks, caps):
    """Blocks a cap could not hold; the last block takes the remainder however big it is."""
    overruns = []
    for index, block in enumerate(blocks):
        records, peak = len(block), peak_batch_bytes(block, caps.max_fetch_gap)
        if ((caps.records and records > caps.records)
                or (caps.batch_bytes and peak > caps.batch_bytes)):
            overruns.append((index, records, peak))
    return overruns


class BlockBudget:

    def __init__(self, caps=None):
        self.caps = caps
        self.records = 0
        self.batch_state = EMPTY_BATCH_STATE

    def reset(self):
        self.records = 0
        self.batch_state = EMPTY_BATCH_STATE

    def would_exceed(self, group):
        if self.caps is None or not self.records:
            return False
        if self.caps.records and self.records + len(group) > self.caps.records:
            return True
        if self.caps.batch_bytes:
            peak = _batch_state(self.batch_state, group, self.caps.max_fetch_gap)[0]
            return peak > self.caps.batch_bytes
        return False

    def add(self, group):
        self.records += len(group)
        if self.caps is not None and self.caps.batch_bytes:
            self.batch_state = _batch_state(self.batch_state, group, self.caps.max_fetch_gap)
