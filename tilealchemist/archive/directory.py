"""A PMTiles archive's directory entries for a zoom range, in offset order.

Pure: the caller supplies the leaf directories' bytes, as archive/local.py
does from a mapped file.
"""
import sys
from collections.abc import Callable, Sequence
from typing import TypeAlias

import pmtiles.tile
from pmtiles.tile import HeaderDict, deserialize_directory, deserialize_header

from tilealchemist.archive.zoom import ZoomLevel, tile_id_bounds
from tilealchemist.console.throttle import UpdateLineThrottle

HEADER_LENGTH: int = 127

# PMTiles v3 section 4 puts header and root inside the first 16 KiB.
HEADER_AND_ROOT_LENGTH: int = 16 * 1024

LOG_INTERVAL: float = 1.0

# A directory entry, as pmtiles models it.
DirectoryEntry: TypeAlias = pmtiles.tile.Entry

# Returns the bytes of the leaf directory an entry points at.
LeafReader: TypeAlias = Callable[[DirectoryEntry], bytes]


class WalkProgress:
    """Prints the directory walk's progress at most once per interval.

    Attributes:
        total_bytes: The leaf directory bytes there are to decode.
        entries: The walk's entries so far.
        frontier: Directories found but not decoded yet.
        directories_decoded: Directories decoded, root included.
        decoded_bytes: Leaf directory bytes decoded.
        throttle: Limits the lines.
    """

    total_bytes: int
    entries: list[DirectoryEntry]
    frontier: list[DirectoryEntry]
    directories_decoded: int
    decoded_bytes: int
    throttle: UpdateLineThrottle

    def __init__(self, total_bytes: int, entries: list[DirectoryEntry],
                 frontier: list[DirectoryEntry]) -> None:
        self.total_bytes = total_bytes
        self.entries = entries
        self.frontier = frontier
        self.directories_decoded = 1
        self.decoded_bytes = 0
        self.throttle = UpdateLineThrottle(LOG_INTERVAL)

    def decoded(self, directory_bytes: bytes) -> None:
        """Counts one more decoded directory and reports if due."""
        self.directories_decoded += 1
        self.decoded_bytes += len(directory_bytes)
        self.report()

    def report(self) -> None:
        """Prints the walk's progress, if a line is due."""
        if not self.throttle.due():
            return
        percent: str = (f" ({100 * self.decoded_bytes / self.total_bytes:.1f}%)"
                        if self.total_bytes else "")
        found: int = self.directories_decoded + len(self.frontier)
        print(f"update: decoded {self.directories_decoded} of {found} "
              f"directories, {len(self.entries)} entries so far{percent}",
              file=sys.stderr)


def parse_header_and_root(prefix: bytes
                          ) -> tuple[HeaderDict, list[pmtiles.tile.Entry]]:
    """Returns the header and root directory in an archive's first bytes.

    Args:
        prefix: The archive's first HEADER_AND_ROOT_LENGTH bytes, or all of a
            shorter one.

    Raises:
        RuntimeError: If the root runs past them.
    """
    header: HeaderDict = deserialize_header(prefix[:HEADER_LENGTH])
    root_start: int = header["root_offset"]
    root_end: int = root_start + header["root_length"]
    if root_end > len(prefix):
        raise RuntimeError(
            f"root directory runs to byte {root_end}, past the {len(prefix)} "
            f"read: PMTiles v3 section 4 requires header plus root inside "
            f"the first {HEADER_AND_ROOT_LENGTH} bytes")
    return header, deserialize_directory(prefix[root_start:root_end])


def _next_tile_id(directory: Sequence[DirectoryEntry], index: int,
                  tile_id_limit: int) -> int:
    """Returns the next entry's tile id, or `tile_id_limit` after the last."""
    if index + 1 < len(directory):
        next_tile_id: int = directory[index + 1].tile_id
        return next_tile_id
    return tile_id_limit


def walk_directory_tree(root_directory: Sequence[DirectoryEntry],
                        read_leaf: LeafReader, leaf_length: int,
                        tile_id_start: int,
                        tile_id_limit: int) -> list[DirectoryEntry]:
    """Returns every entry overlapping `[tile_id_start, tile_id_limit)`.

    Entries straddling a bound come back whole.

    Args:
        root_directory: The archive's root directory.
        read_leaf: Where the leaf directories come from.
        leaf_length: The leaf directory bytes there are, for progress.
        tile_id_start: The first tile id of the range.
        tile_id_limit: The tile id after the range.
    """
    entries: list[DirectoryEntry] = []
    # Leaf pointers, each decoded when popped, so decoding keeps pace with
    # the walk.
    frontier: list[DirectoryEntry] = []
    progress: WalkProgress = WalkProgress(leaf_length, entries, frontier)
    directory: Sequence[DirectoryEntry] = root_directory

    index: int
    entry: DirectoryEntry
    while True:
        for index, entry in enumerate(directory):
            if entry.tile_id >= tile_id_limit:
                break
            if entry.run_length == 0:
                next_tile_id: int = _next_tile_id(directory, index,
                                                  tile_id_limit)
                if next_tile_id > tile_id_start:
                    frontier.append(entry)
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)
        if not frontier:
            return entries
        directory_bytes: bytes = read_leaf(frontier.pop())
        directory = deserialize_directory(directory_bytes)
        progress.decoded(directory_bytes)


def entries_in_range(header: HeaderDict,
                     root_directory: Sequence[DirectoryEntry],
                     read_leaf: LeafReader, leaf_length: int,
                     min_zoom: ZoomLevel,
                     max_zoom: ZoomLevel) -> list[DirectoryEntry]:
    """Returns the entries of a zoom range, from the root and its leaves.

    Args:
        header: The archive's header.
        root_directory: The archive's root directory.
        read_leaf: Where the leaf directories come from.
        leaf_length: The leaf directory bytes there are, for progress.
        min_zoom: The range's lowest zoom level.
        max_zoom: The range's highest zoom level.

    Returns:
        The entries in offset order, lowest tile id first within an offset.
    """
    tile_id_start: int
    tile_id_limit: int
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    print(f"starting decode ({header['tile_entries_count']} entries "
          f"expected)", file=sys.stderr)
    entries: list[DirectoryEntry] = walk_directory_tree(
        root_directory, read_leaf, leaf_length, tile_id_start, tile_id_limit)
    # Puts the entries of one blob next to each other.
    entries.sort(key=lambda entry: (entry.offset, entry.tile_id))
    return entries

