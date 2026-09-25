"""The CPU-bound half of a worker; see docs/ARCHITECTURE.md "Parallelism"."""
import sys
import time

from pmtiles.tile import tileid_to_zxy

from tilealchemist.pmtiles_index import tile_id_bounds
from tilealchemist.tile import Tile
from tilealchemist.throttle import UpdateLineThrottle


DEFAULT_REPORT_INTERVAL = 60.0


class TransformProgress:
    """Prints how far a transform has got, at most once per interval.

    Attributes:
        total_entries: How many entries the transform will walk.
        label: What to call this transform in the line.
        processed: How many entries it has walked so far.
        throttle: The rate limiter the lines go through.
    """

    def __init__(self, total_entries, interval, label="transforming tiles"):
        """Set up progress reporting for one transform.

        Args:
            total_entries: How many entries the transform will walk.
            interval: Minimum seconds between two lines.
            label: What to call this transform in the line.
        """
        self.total_entries = total_entries
        self.label = label
        self.processed = 0
        self.throttle = UpdateLineThrottle(interval)

    def tick(self, tile):
        """Record one more entry walked, and report if a line is due.

        Args:
            tile: The `(zoom, column, row)` just reached, to say roughly where
                the transform has got to.
        """
        self.processed += 1
        if not self.throttle.due():
            return
        percent = (100 * self.processed / self.total_entries) if self.total_entries else 100.0
        zoom, tile_column, tile_row = tile
        print(f"update: {self.label}: "
              f"{self.processed}/{self.total_entries} ({percent:.1f}%), "
              f"currently around tile z{zoom}/x{tile_column}/y{tile_row}",
              file=sys.stderr)


def _describe_entry(entry):
    """Name an entry the way an error should refer to it.

    Args:
        entry: The entry to describe.

    Returns:
        A phrase such as ``tile 4/2/3 (run of 12 tiles)``.
    """
    zoom, column, row = tileid_to_zxy(entry.tile_id)
    if entry.run_length == 1:
        return f"tile {zoom}/{column}/{row}"
    return f"tile {zoom}/{column}/{row} (run of {entry.run_length} tiles)"


def _entry_outputs(tile_data, entry, profiles, schema, usage):
    """Decode one entry's tile and run every profile over it.

    Args:
        tile_data: The tile's stored bytes.
        entry: The entry those bytes came from, named in any error.
        profiles: The profiles to run, in output order.
        schema: The schema the tile is encoded in.
        usage: The accounting to charge the decode and the transform to.

    Returns:
        One output per profile, in the same order, None where a profile
        skipped the tile.

    Raises:
        RuntimeError: If a profile raised, naming the profile and the tile.
    """
    decode_start = time.perf_counter()
    tile = Tile.decode(tile_data, schema)
    transform_start = time.perf_counter()
    outputs = []
    for profile in profiles:
        try:
            outputs.append(profile.transform_tile(tile))
        except Exception as error:
            raise RuntimeError(
                f"profile {profile.name!r} failed on {_describe_entry(entry)}") from error
    usage.add_decode(len(tile_data), transform_start - decode_start,
                     time.perf_counter() - transform_start)
    return outputs


def transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress, profiles,
                                schema, usage):
    """Transform one fetched batch, for every profile at once.

    Entries arrive in offset order, which puts duplicate bytes next to each
    other, so a single check before the decode dedupes the work for all the
    profiles together.

    Args:
        blob: The batch's fetched bytes.
        batch: The `(offset, length, entries)` batch they came from.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.
        transform_progress: The progress reporter to tick.
        profiles: The profiles to run, in output order.
        schema: The schema the source tiles are in.
        usage: The accounting to charge the work to.

    Returns:
        One list of `(tile_id, run_length, payload)` runs per profile, in the
        same order, clipped to the run's zoom range.
    """
    batch_offset, _batch_length, batch_entries = batch
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    results = [[] for _ in profiles]
    previous_key = None
    outputs = None
    for entry in batch_entries:
        # Offset order puts duplicate bytes adjacent, so one check dedupes for every profile.
        key = (entry.offset, entry.length)
        if key != previous_key:
            start = entry.offset - batch_offset
            outputs = _entry_outputs(blob[start:start + entry.length], entry, profiles, schema,
                                      usage)
            previous_key = key
        usage.entries += 1
        transform_progress.tick(tileid_to_zxy(entry.tile_id))
        run_start = max(entry.tile_id, tile_id_start)
        run_length = min(entry.tile_id + entry.run_length, tile_id_limit) - run_start
        if run_length <= 0:
            continue
        usage.output_tiles += run_length
        for profile_results, output_data in zip(results, outputs):
            profile_results.append((run_start, run_length, output_data))
    return results
