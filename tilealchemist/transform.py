"""The CPU-bound half of a worker; see docs/ARCHITECTURE.md "Parallelism"."""
import sys
import time

from pmtiles.tile import tileid_to_zxy

from tilealchemist.pmtiles_index import tile_id_bounds
from tilealchemist.tile import Tile
from tilealchemist.throttle import UpdateLineThrottle


DEFAULT_REPORT_INTERVAL = 60.0


class TransformProgress:
    def __init__(self, total_entries, interval, label="transforming tiles"):
        self.total_entries = total_entries
        self.label = label
        self.processed = 0
        self.throttle = UpdateLineThrottle(interval)

    def tick(self, tile):
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
    zoom, column, row = tileid_to_zxy(entry.tile_id)
    if entry.run_length == 1:
        return f"tile {zoom}/{column}/{row}"
    return f"tile {zoom}/{column}/{row} (run of {entry.run_length} tiles)"


def _entry_outputs(tile_data, entry, profiles, schema, usage):
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
