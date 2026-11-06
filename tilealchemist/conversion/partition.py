"""Cutting entries into shares of equal blob bytes, as `convert` cuts chunks.

Bytes track the work: each blob is read, decoded, transformed and written
once, and tiles range from ~100 B of ocean to ~300 KB of city, while a
duplicate costs one written entry. On a planet run, blob count explained
none of a chunk's time (R² 0.00), bytes 0.44.
"""
from collections.abc import Iterator, Sequence

from tilealchemist.archive.directory import DirectoryEntry


def _decoding_entries(entries: Sequence[DirectoryEntry]
                      ) -> Iterator[tuple[int, DirectoryEntry]]:
    """Yields `(index, entry)` for the first entry of each blob.

    Args:
        entries: Entries in offset order.
    """
    previous_key: tuple[int, int] | None = None
    index: int
    entry: DirectoryEntry
    for index, entry in enumerate(entries):
        key: tuple[int, int] = (entry.offset, entry.length)
        if key != previous_key:
            previous_key = key
            yield index, entry


def count_decoding_entries(entries: Sequence[DirectoryEntry]) -> int:
    """Returns how many blobs entries in offset order point at."""
    return sum(1 for _ in _decoding_entries(entries))


def count_blob_bytes(entries: Sequence[DirectoryEntry]) -> int:
    """Returns the bytes of the blobs entries in offset order point at."""
    return sum(entry.length for _, entry in _decoding_entries(entries))


def share_bounds(entries: Sequence[DirectoryEntry],
                 share_count: int) -> list[int]:
    """Cuts entries into shares holding equally many blob bytes.

    A share starts at a blob's first entry and keeps every entry of that
    blob, so each blob is read and decoded once. Each blob weighs its
    length once; a blob without bytes weighs nothing.

    Args:
        entries: Entries in offset order.
        share_count: How many shares to cut.

    Returns:
        `share_count + 1` ascending indices: share `i` is
        `entries[bounds[i]:bounds[i + 1]]`. Shares the blobs run out before
        are empty, at the end.
    """
    total: int = count_blob_bytes(entries)
    bounds: list[int] = [0]
    seen: int = 0
    index: int
    entry: DirectoryEntry
    for index, entry in _decoding_entries(entries):
        # Share j starts at the first blob with total * j / share_count bytes
        # before it. Without any bytes, everything stays in share 0.
        if (seen and len(bounds) < share_count
                and seen * share_count >= total * len(bounds)):
            bounds.append(index)
        seen += entry.length
    return bounds + [len(entries)] * (share_count + 1 - len(bounds))

