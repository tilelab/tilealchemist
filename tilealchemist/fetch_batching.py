"""Grouping a worker's real manifest entries into range-GET batches."""
import contextlib
import mmap
import os
import sys
from collections import namedtuple

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range

# 8 MB: below this, skipping unread bytes on an open connection beats another round trip.
DEFAULT_MAX_FETCH_GAP = 8 * 1024 * 1024

# One range request's worth of entries; see docs/ARCHITECTURE.md "Fetching".
Batch = namedtuple("Batch", "offset length entries")


def plan_fetch_batches(real_entries, max_fetch_gap):
    """Entries as one Batch per range request."""
    return [_batch(entries) for entries in _split_on_wide_holes(real_entries, max_fetch_gap)]


def peak_batch_bytes(entries, max_fetch_gap=DEFAULT_MAX_FETCH_GAP):
    """The largest single range request these entries will need, by plan_fetch_batches' rule.

    Measured rather than planned: the planner asks this of whole blocks it
    has not batched and will not fetch, so it walks the entries by the same
    rule _split_on_wide_holes() splits on without building the batches.

    Args:
        entries: The entries to measure, in walk order; a gap record carries
            no bytes and is skipped.
        max_fetch_gap: The largest gap that still shares one fetch.

    Returns:
        The widest span any one batch will reach.
    """
    peak = reach = 0
    start = None
    for entry in entries:
        if entry.length == 0:
            continue
        if start is None or entry.offset - reach > max_fetch_gap:
            start = reach = entry.offset
        reach = max(reach, entry.offset + entry.length)
        peak = max(peak, reach - start)
    return peak


def _split_on_wide_holes(real_entries, max_fetch_gap):
    """Cut entries apart where the unread gap between them is too wide.

    Args:
        real_entries: The worker's real entries, in offset order.
        max_fetch_gap: The largest gap still worth reading through rather than
            opening a second request.

    Yields:
        Runs of entries, each to be fetched in one range request.
    """
    entries, reach = [], 0
    for entry in real_entries:
        # A running end, not the previous entry's: a long entry can reach past a later one.
        if entries and entry.offset - reach > max_fetch_gap:
            yield entries
            entries, reach = [], 0
        entries.append(entry)
        reach = max(reach, entry.offset + entry.length)
    if entries:
        yield entries


def _batch(batch_entries):
    """Measure the range one run of entries needs.

    Args:
        batch_entries: Entries that will share one request, in offset order.

    Returns:
        A Batch whose length reaches the furthest end any entry in the run has.
    """
    batch_offset = batch_entries[0].offset
    batch_length = max(entry.offset + entry.length for entry in batch_entries) - batch_offset
    return Batch(batch_offset, batch_length, batch_entries)


@contextlib.contextmanager
def fetch_batch_blob(session, batch, batch_label, worker_index, source, report_interval,
                     spool_dir, phases):
    """Fetch one batch's bytes, once, for every profile in the run to share.

    The body is streamed to a file and handed back as a read-only mapping, so
    a batch costs page cache the kernel can reclaim rather than resident heap.
    Slicing a mapping is what slicing bytes was, which is all any caller does
    with it.

    Args:
        session: The requests session the fetches share.
        batch: A Batch from plan_fetch_batches().
        batch_label: Suffix naming this batch in the log lines.
        worker_index: This worker's number, for retry logging.
        source: The archive's SourceMetadata.
        report_interval: Seconds between download progress lines.
        spool_dir: Directory to spool the body into. It belongs beside the
            shards, not in a tmpfs, where the bytes would stay in RAM.
        phases: The worker's PhaseSeconds, charged for the download alone.
            Mapping and unmapping are not fetching, and the caller's block
            holds this context open across the whole transform.

    Yields:
        The batch's bytes, as a read-only mmap.
    """
    progress = DownloadProgress(batch.length, report_interval, f"tile data{batch_label}")

    print(f"starting download{batch_label} ({batch.length} bytes, {len(batch.entries)} entries "
          f"in a single range request)", file=sys.stderr)
    spool_path = os.path.join(spool_dir, f"batch-{worker_index}.blob")
    try:
        with phases.phase("fetch"):
            fetch_range(
                session, source.url, source.tile_data_offset + batch.offset, batch.length,
                retry_label=f"worker {worker_index}", on_chunk=progress.update,
                dest_path=spool_path)
        with open(spool_path, "rb") as spooled:
            with mmap.mmap(spooled.fileno(), 0, access=mmap.ACCESS_READ) as blob:
                yield blob
    finally:
        # Before the next batch: two batches' bytes at once is what the budget forbids.
        with contextlib.suppress(FileNotFoundError):
            os.remove(spool_path)
