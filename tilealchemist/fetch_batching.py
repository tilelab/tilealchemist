"""Grouping a worker's real manifest entries into range-GET batches."""
import sys

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range

# 8 MB: below this, skipping unread bytes on an open connection beats another round trip.
DEFAULT_MAX_FETCH_GAP = 8 * 1024 * 1024


def plan_fetch_batches(real_entries, max_fetch_gap):
    """Entries as one (offset, length, entries) batch per range request."""
    return [_batch(entries) for entries in _split_on_wide_holes(real_entries, max_fetch_gap)]


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
        `(offset, length, entries)`, whose length reaches the furthest end any
        entry in the run has.
    """
    batch_offset = batch_entries[0].offset
    batch_length = max(entry.offset + entry.length for entry in batch_entries) - batch_offset
    return batch_offset, batch_length, batch_entries


def fetch_batch_blob(session, batch, batch_label, worker_index, source, report_interval):
    """Fetch one batch's bytes, once, for every profile in the run to share.

    Args:
        session: The requests session the fetches share.
        batch: An `(offset, length, entries)` batch from plan_fetch_batches().
        batch_label: Suffix naming this batch in the log lines.
        worker_index: This worker's number, for retry logging.
        source: The archive's SourceMetadata.
        report_interval: Seconds between download progress lines.

    Returns:
        The batch's raw bytes.
    """
    batch_offset, batch_length, batch_entries = batch
    progress = DownloadProgress(batch_length, report_interval, f"tile data{batch_label}")

    print(f"starting download{batch_label} ({batch_length} bytes, {len(batch_entries)} entries "
          f"in a single range request)", file=sys.stderr)
    return fetch_range(
        session, source.url, source.tile_data_offset + batch_offset, batch_length,
        retry_label=f"worker {worker_index}", on_chunk=progress.update)
