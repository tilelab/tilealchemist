"""One shard database per profile, and the writes that fill it."""
import json
import sqlite3
import sys

from pmtiles.tile import tileid_to_zxy

SHARD_LAYOUTS = ("flat", "dedup")

COMPLETE_KEY = "tilealchemist_complete"

INSERT_TILE = ("INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
               "VALUES (?, ?, ?, ?)")
INSERT_MAP = ("INSERT INTO map (zoom_level, tile_column, tile_row, tile_id) "
              "VALUES (?, ?, ?, ?)")
INSERT_IMAGE = "INSERT INTO images (tile_id, tile_data) VALUES (?, ?)"

FLAT_SCHEMA = (
    "CREATE TABLE tiles ("
    "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)",
    "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)",
)
DEDUP_SCHEMA = (
    "CREATE TABLE map ("
    "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id INTEGER)",
    "CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row)",
    "CREATE TABLE images (tile_id INTEGER PRIMARY KEY, tile_data BLOB)",
    "CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, "
    "map.tile_column AS tile_column, map.tile_row AS tile_row, "
    "images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id",
)


class ProfileTileCounts:
    """Running totals for one profile's shard.

    Attributes:
        written: Tiles written.
        skipped: Tiles the profile left out.
        blobs: Distinct tile payloads written.
    """

    def __init__(self):
        """Start every total at zero."""
        self.written = 0
        self.skipped = 0
        self.blobs = 0

    def add(self, written, skipped, blobs):
        """Fold one write's totals in.

        Args:
            written: Tiles written.
            skipped: Tiles left out.
            blobs: Distinct payloads written.
        """
        self.written += written
        self.skipped += skipped
        self.blobs += blobs


def _tile_rows(runs):
    """Expand runs into one row per tile, in MBTiles' row order.

    A generator rather than a list: an ocean-sized run must not be built in
    memory before sqlite3 ever sees it.

    Args:
        runs: `(tile_id, run_length, payload)` triples. A None payload is a
            tile the profile left out, and yields nothing.

    Yields:
        `(zoom, column, row, payload)` per tile, the row flipped into
        MBTiles' bottom-up numbering.
    """
    for tile_id, run_length, payload in runs:
        if payload is None:
            continue
        for run_offset in range(run_length):
            zoom, tile_column, tile_row = tileid_to_zxy(tile_id + run_offset)
            yield (zoom, tile_column, (2 ** zoom - 1) - tile_row, payload)


def _run_counts(runs):
    """Count what a set of runs will have written.

    Args:
        runs: `(tile_id, run_length, payload)` triples, in write order.

    Returns:
        `(written, skipped, blobs)`, where blobs counts payloads differing
        from the one before rather than distinct payloads overall.
    """
    written = skipped = blobs = 0
    # A strong reference for the whole loop, so `is` never meets a reused address.
    previous_data = None
    for _tile_id, run_length, output_data in runs:
        if output_data is None:
            skipped += run_length
            continue
        if output_data is not previous_data:
            blobs += 1
            previous_data = output_data
        written += run_length
    return written, skipped, blobs


class ShardWriter:
    """One profile's shard, written either as a flat `tiles` table or as map + images."""

    def __init__(self, connection, layout):
        """Wrap an open shard database.

        Args:
            connection: The sqlite3 connection to write through.
            layout: Either "flat" or "dedup".
        """
        self.connection = connection
        self.layout = layout
        self.image_id = 0

    def write(self, runs):
        """Write runs into the shard.

        Args:
            runs: `(tile_id, run_length, payload)` triples, in write order.

        Returns:
            `(written, skipped, blobs)` for what was just written.
        """
        if self.layout == "dedup":
            self._write_dedup(runs)
        else:
            self.connection.executemany(INSERT_TILE, _tile_rows(runs))
        return _run_counts(runs)

    def _write_dedup(self, runs):
        """Write runs as map rows pointing at shared image blobs.

        Args:
            runs: `(tile_id, run_length, payload)` triples, in write order.
                Consecutive runs sharing a payload share one image row.
        """
        images, id_runs = [], []
        previous_data = None
        for tile_id, run_length, output_data in runs:
            if output_data is None:
                continue
            if output_data is not previous_data:
                previous_data = output_data
                self.image_id += 1
                images.append((self.image_id, output_data))
            id_runs.append((tile_id, run_length, self.image_id))
        self.connection.executemany(INSERT_IMAGE, images)
        self.connection.executemany(INSERT_MAP, _tile_rows(id_runs))


def init_mbtiles(path, min_zoom, max_zoom, profile, schema, layout="flat"):
    """Create one profile's shard database, with its metadata.

    Args:
        path: File to create.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.
        profile: The profile whose output goes in it.
        schema: The schema the source tiles are in.
        layout: Either "flat" or "dedup".

    Returns:
        A ShardWriter over the new database.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    for statement in DEDUP_SCHEMA if layout == "dedup" else FLAT_SCHEMA:
        connection.execute(statement)
    vector_layers_json = json.dumps(
        {"vector_layers": profile.vector_layers_json(schema)}, separators=(",", ":"))
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("name", profile.mbtiles_name),
            ("format", "pbf"),
            ("minzoom", str(min_zoom)),
            ("maxzoom", str(max_zoom)),
            ("json", vector_layers_json),
        ],
    )
    connection.commit()
    return ShardWriter(connection, layout)


def write_gap_tiles(gap_entries, writer, output_data):
    """Write the profile's single gap answer at every gap tile.

    Args:
        gap_entries: The gap records this worker carries.
        writer: The shard to write into.
        output_data: The profile's answer for a gap tile, or None to write
            nothing at all.

    Returns:
        `(written, skipped, blobs)` for what was just written.
    """
    runs = [(entry.tile_id, entry.run_length, output_data) for entry in gap_entries]
    written, skipped, blobs = writer.write(runs)
    print(f"gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped, blobs


def close_shards(writers):
    """Mark each shard complete and close it.

    Args:
        writers: The shards to finish. The completion marker is what tells a
            later step the shard was not cut short.
    """
    for writer in writers:
        writer.connection.execute("INSERT INTO metadata (name, value) VALUES (?, ?)",
                                   (COMPLETE_KEY, "1"))
        writer.connection.commit()
        writer.connection.close()
