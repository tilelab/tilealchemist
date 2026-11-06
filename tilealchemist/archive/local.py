"""A PMTiles file on this machine, mapped for `convert` to read."""
import contextlib
import gzip
import json
import mmap
from collections.abc import Iterator
from typing import Any, BinaryIO

from pmtiles.tile import Compression, Entry, HeaderDict, TileType

from tilealchemist.archive.directory import (
    HEADER_AND_ROOT_LENGTH,
    DirectoryEntry,
    entries_in_range,
    parse_header_and_root,
)
from tilealchemist.archive.zoom import ZoomLevel


class LocalArchive:
    """A PMTiles file, mapped read-only.

    Attributes:
        path: The file.
        data: Its bytes.
        header: Its header.
        root_directory: Its root directory.
    """

    path: str
    data: mmap.mmap
    header: HeaderDict
    root_directory: list[Entry]

    def __init__(self, path: str, data: mmap.mmap) -> None:
        self.path = path
        self.data = data
        self.header, self.root_directory = parse_header_and_root(
            data[:HEADER_AND_ROOT_LENGTH])

    def metadata(self) -> Any:
        """Returns the archive's metadata as parsed JSON; {} if it has none."""
        length: int = self.header["metadata_length"]
        if not length:
            return {}
        start: int = self.header["metadata_offset"]
        return json.loads(gzip.decompress(self.data[start:start + length]))

    def entries(self, min_zoom: ZoomLevel,
                max_zoom: ZoomLevel) -> list[DirectoryEntry]:
        """Returns the entries of a zoom range, in offset order.

        Each leaf directory is read from the mapped file where it lies, so
        any leaf order and nesting works.
        """
        return entries_in_range(
            self.header, self.root_directory, self._leaf_bytes,
            self.header["leaf_directory_length"], min_zoom, max_zoom)

    def _leaf_bytes(self, entry: DirectoryEntry) -> bytes:
        """Returns the bytes of the leaf directory `entry` points at."""
        start: int = self.header["leaf_directory_offset"] + entry.offset
        return self.data[start:start + entry.length]


@contextlib.contextmanager
def open_archive(path: str) -> Iterator[LocalArchive]:
    """Maps the PMTiles file at `path` for reading.

    Raises:
        ValueError: If its tiles aren't gzipped MVT, its directories aren't
            gzipped, or the file is shorter than its header says.
    """
    file: BinaryIO
    data: mmap.mmap
    with (open(path, "rb") as file,
          mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data):
        archive: LocalArchive = LocalArchive(path, data)
        header: HeaderDict = archive.header
        if (header["tile_type"] != TileType.MVT
                or header["tile_compression"] != Compression.GZIP
                or header["internal_compression"] != Compression.GZIP):
            raise ValueError(
                f"{path} holds {header['tile_type'].name} tiles with "
                f"{header['tile_compression'].name} tile and "
                f"{header['internal_compression'].name} directory "
                f"compression; tilealchemist reads gzipped MVT with gzipped "
                f"directories")
        end: int = max(
            header["root_offset"] + header["root_length"],
            header["metadata_offset"] + header["metadata_length"],
            header["leaf_directory_offset"] + header["leaf_directory_length"],
            header["tile_data_offset"] + header["tile_data_length"])
        if len(data) < end:
            raise ValueError(
                f"{path} is {len(data)} bytes, but its header places data up "
                f"to byte {end}: the file is cut short, as by an interrupted "
                f"download")
        yield archive
