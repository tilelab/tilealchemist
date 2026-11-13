"""Transforming one chunk of entries."""
import mmap
import sys
from collections.abc import Sequence
from typing import NamedTuple, TypeAlias

from pmtiles.tile import tileid_to_zxy

from tilealchemist.archive.directory import DirectoryEntry
from tilealchemist.archive.zoom import ZoomLevel, tile_id_bounds
from tilealchemist.console.throttle import UpdateLineThrottle
from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.tile import Tile
from tilealchemist.schemas.base import TileSchema

# Tile data to transform: a chunk's copy, or a mapped file.
TileData: TypeAlias = bytes | mmap.mmap

# Seconds between transform progress lines, per process.
REPORT_INTERVAL: float = 60.0

# `(tile_id, run_length, payload)`; the payload is None where the profile
# left the tile out.
TileRun: TypeAlias = tuple[int, int, bytes | None]

# One list of runs per profile.
ChunkResults: TypeAlias = list[list[TileRun]]


class TransformSettings(NamedTuple):
    """What every chunk of a run shares; transform_pool.ChunkJob pickles it."""

    min_zoom: ZoomLevel
    max_zoom: ZoomLevel
    profiles: list[Profile]
    schema: TileSchema


class TransformProgress:
    """Prints a transform's progress at most once per interval.

    Attributes:
        total_entries: Entries the transform walks.
        label: The transform's name in the line.
        processed: Entries walked so far.
        throttle: Limits the lines.
    """

    total_entries: int
    label: str
    processed: int
    throttle: UpdateLineThrottle

    def __init__(self, total_entries: int, interval: float,
                 label: str) -> None:
        self.total_entries = total_entries
        self.label = label
        self.processed = 0
        self.throttle = UpdateLineThrottle(interval)

    def tick(self, tile: tuple[int, int, int]) -> None:
        """Counts one more entry, at `(zoom, x, y)`, and reports if due."""
        self.processed += 1
        if not self.throttle.due():
            return
        percent: float = ((100 * self.processed / self.total_entries)
                          if self.total_entries else 100.0)
        zoom: int
        tile_column: int
        tile_row: int
        zoom, tile_column, tile_row = tile
        print(f"update: {self.label}: "
              f"{self.processed}/{self.total_entries} ({percent:.1f}%), "
              f"currently around tile z{zoom}/x{tile_column}/y{tile_row}",
              file=sys.stderr)


def _describe_entry(entry: DirectoryEntry) -> str:
    """Returns the entry as errors name it, such as `tile 4/2/3`."""
    zoom: int
    column: int
    row: int
    zoom, column, row = tileid_to_zxy(entry.tile_id)
    if entry.run_length == 1:
        return f"tile {zoom}/{column}/{row}"
    return f"tile {zoom}/{column}/{row} (run of {entry.run_length} tiles)"


def _entry_outputs(tile_data: bytes, entry: DirectoryEntry,
                   profiles: list[Profile],
                   schema: TileSchema) -> list[bytes | None]:
    """Returns each profile's output for one tile, None where it left it out.

    Args:
        tile_data: The tile's bytes.
        entry: Where they came from, for errors.
        profiles: The profiles, in output order.
        schema: The tile's schema.

    Raises:
        RuntimeError: If a profile raised, naming it and the tile.
    """
    tile: Tile = Tile.decode(tile_data, schema)
    outputs: list[bytes | None] = []
    profile: Profile
    for profile in profiles:
        try:
            outputs.append(profile.build_tile(tile))
        except Exception as error:
            raise RuntimeError(
                f"profile {profile.name!r} failed on "
                f"{_describe_entry(entry)}") from error
    return outputs


def transform_chunk(buffer: TileData, buffer_offset: int,
                    entries: Sequence[DirectoryEntry],
                    settings: TransformSettings,
                    label: str) -> ChunkResults:
    """Runs every profile over one chunk.

    Entries in offset order put a blob's entries together, so each blob is
    decoded once for all profiles.

    Args:
        buffer: The bytes the entries point into.
        buffer_offset: The tile data offset `buffer` starts at.
        entries: The chunk's entries, in offset order.
        settings: The run's settings.
        label: The chunk's name in progress lines.

    Returns:
        Each profile's runs, clipped to the run's zoom range.
    """
    profiles: list[Profile] = settings.profiles
    progress: TransformProgress = TransformProgress(len(entries),
                                                    REPORT_INTERVAL, label)
    tile_id_start: int
    tile_id_limit: int
    tile_id_start, tile_id_limit = tile_id_bounds(settings.min_zoom,
                                                  settings.max_zoom)
    results: ChunkResults = [[] for _ in profiles]
    previous_key: tuple[int, int] | None = None
    outputs: list[bytes | None] = []
    entry: DirectoryEntry
    profile_results: list[TileRun]
    output_data: bytes | None
    for entry in entries:
        key: tuple[int, int] = (entry.offset, entry.length)
        if key != previous_key:
            start: int = entry.offset - buffer_offset
            outputs = _entry_outputs(buffer[start:start + entry.length], entry,
                                     profiles, settings.schema)
            previous_key = key
        progress.tick(tileid_to_zxy(entry.tile_id))
        # Clipped here, so nothing downstream is O(run_length).
        run_start: int = max(entry.tile_id, tile_id_start)
        run_end: int = min(entry.tile_id + entry.run_length, tile_id_limit)
        run_length: int = run_end - run_start
        if run_length <= 0:
            continue
        for profile_results, output_data in zip(results, outputs):
            profile_results.append((run_start, run_length, output_data))
    return results
