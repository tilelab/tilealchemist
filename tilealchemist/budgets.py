"""Hard per-worker limits, as limits rather than prices; see docs/ARCHITECTURE.md "Budgets"."""
from collections import namedtuple

from tilealchemist.fetch_batching import DEFAULT_MAX_FETCH_GAP

# One Entry namedtuple in a list, measured; see docs/ARCHITECTURE.md "Budgets".
MANIFEST_RECORD_BYTES = 184

DEFAULT_MANIFEST_RAM_BYTES = 512 * 1024 * 1024
DEFAULT_PEAK_BATCH_BYTES = 1024 * 1024 * 1024

Caps = namedtuple("Caps", "records batch_bytes max_fetch_gap")

EMPTY_BATCH_STATE = (0, None, 0)


def caps_from_budgets(manifest_ram_bytes, peak_batch_bytes,
                      max_fetch_gap=DEFAULT_MAX_FETCH_GAP):
    """Turn byte budgets into the per-block caps a partition can check.

    Args:
        manifest_ram_bytes: RAM a worker may spend holding its manifest, or 0
            for no limit.
        peak_batch_bytes: Bytes a single range request may reach, or 0 for no
            limit.
        max_fetch_gap: The largest gap between two entries that still shares
            one fetch.

    Returns:
        Those limits as a Caps.
    """
    return Caps(records=manifest_ram_bytes // MANIFEST_RECORD_BYTES if manifest_ram_bytes else 0,
                batch_bytes=peak_batch_bytes or 0, max_fetch_gap=max_fetch_gap)


def _batch_state(state, entries, max_fetch_gap):
    """Carry the running batch measurement across further entries.

    Args:
        state: The `(peak, start, reach)` reached so far.
        entries: Further entries, in walk order; gap records are skipped.
        max_fetch_gap: The largest gap that still shares one fetch.

    Returns:
        The updated `(peak, start, reach)`.
    """
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
    """Tracks what a block has taken, so a partition can stop before a cap.

    `would_exceed()` asks about a group without taking it, `add()` takes it,
    and `reset()` starts the next block.

    Attributes:
        caps: The limits to hold a block to, or None for none.
        records: How many records the current block has taken.
        batch_state: The current block's running `(peak, start, reach)` batch
            measurement.
    """

    def __init__(self, caps=None):
        """Start an empty block.

        Args:
            caps: The limits to hold each block to, or None for none.
        """
        self.caps = caps
        self.records = 0
        self.batch_state = EMPTY_BATCH_STATE

    def reset(self):
        """Forget the current block and start the next one empty."""
        self.records = 0
        self.batch_state = EMPTY_BATCH_STATE

    def would_exceed(self, group):
        """Ask whether taking this group would put the block over a cap.

        Args:
            group: The records that would be added next.

        Returns:
            True if the block already holds something and the group would break
            the record cap or the peak batch cap. An empty block takes a group
            however large it is, since something has to.
        """
        if self.caps is None or not self.records:
            return False
        if self.caps.records and self.records + len(group) > self.caps.records:
            return True
        if self.caps.batch_bytes:
            peak = _batch_state(self.batch_state, group, self.caps.max_fetch_gap)[0]
            return peak > self.caps.batch_bytes
        return False

    def add(self, group):
        """Take a group into the current block.

        Args:
            group: The records to add.
        """
        self.records += len(group)
        if self.caps is not None and self.caps.batch_bytes:
            self.batch_state = _batch_state(self.batch_state, group, self.caps.max_fetch_gap)
