"""One PMTiles part per profile, and the writes that fill it.

A part is a complete, clustered PMTiles archive holding one worker's share of
one profile's layer. The merge job joins every worker's part with go-pmtiles'
`pmtiles merge`, which never decodes a tile; docs/ARCHITECTURE.md "Part
format" says what that asks of a part and why.
"""
import array
import gzip
import hashlib
import json
import mmap
import os
import sys
import tempfile

import numpy as np
from pmtiles.tile import (
    Compression,
    Entry,
    TileType,
    serialize_header,
    tileid_to_zxy,
)
from pmtiles.writer import optimize_directories

HEADER_LENGTH = 127

# PMTiles v3 section 4 requires header plus root inside the first 16,384 bytes.
ROOT_DIRECTORY_LIMIT = 16 * 1024 - HEADER_LENGTH

# Web Mercator's latitude limit. Every part claims the whole world, whatever
# share of it it holds, and the same center point: `pmtiles merge` takes the
# union of the inputs' bounds and the first input's center, so identical
# headers make the merged one independent of which part happens to come
# first. The center *zoom* cannot be shared the same way, `pmtiles verify`
# requiring it inside each part's own zoom range; see _write_archive().
WORLD_BOUNDS_E7 = {
    "min_lon_e7": -180 * 10_000_000,
    "min_lat_e7": -850_511_287,
    "max_lon_e7": 180 * 10_000_000,
    "max_lat_e7": 850_511_287,
    "center_lon_e7": 0,
    "center_lat_e7": 0,
}

# Enough to tell two different payloads apart across a whole planet, at 16 B a
# key rather than the payload itself held alive as one.
DIGEST_SIZE = 16

# Bytes copied out of the spool per write once a part is laid out.
COPY_STEP_BYTES = 64 * 2 ** 20


class ProfileTileCounts:
    """Running totals for one profile's part.

    Attributes:
        written: Tiles written.
        skipped: Tiles the profile left out.
        blobs: Payloads differing from the one before; see _run_counts().
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
            blobs: Payloads differing from the one before.
        """
        self.written += written
        self.skipped += skipped
        self.blobs += blobs


def _run_counts(runs):
    """Count what a set of runs will have written.

    Args:
        runs: `(tile_id, run_length, payload)` triples, in write order.

    Returns:
        `(written, skipped, blobs)`, where blobs counts payloads differing
        from the one before rather than distinct payloads overall.
    """
    written = skipped = blobs = 0
    # A strong reference for the whole loop, so `is` never meets a reused
    # address.
    previous_data = None
    for unused_tile_id, run_length, output_data in runs:
        if output_data is None:
            skipped += run_length
            continue
        if output_data is not previous_data:
            blobs += 1
            previous_data = output_data
        written += run_length
    return written, skipped, blobs


class PartWriter:
    """One profile's part: runs collected as written, laid out at finalize().

    Runs arrive in source-offset order, which is not tile id order wherever
    the source archive deduplicated. They are therefore only recorded here,
    each distinct payload spooled to disk once, and sorted into a clustered
    archive by finalize().

    Attributes:
        path: The part file finalize() writes.
        metadata: The part's JSON metadata, which `pmtiles merge` copies into
            the layer from whichever part comes first.
        center_zoom: The zoom the header's center is meant for, clamped into
            the part's own zoom range when it is written.
    """

    def __init__(self, path, metadata, center_zoom):
        """Start an empty part.

        Args:
            path: The part file finalize() writes. Nothing is created there
                before then.
            metadata: The part's JSON metadata.
            center_zoom: The zoom the header's center is meant for.
        """
        self.path = path
        self.metadata = metadata
        self.center_zoom = center_zoom
        self._spool = tempfile.TemporaryFile(
            dir=os.path.dirname(os.path.abspath(path)))
        self._spool_bytes = 0
        self._blob_offsets = array.array("Q")
        self._blob_lengths = array.array("Q")
        self._blob_index = {}
        self._tile_ids = array.array("Q")
        self._run_lengths = array.array("Q")
        self._run_blobs = array.array("Q")
        # A strong reference across writes, so `is` never meets a reused
        # address; see _blob_for().
        self._previous_data = None
        self._previous_blob = -1

    def write(self, runs):
        """Record runs for the part.

        Args:
            runs: `(tile_id, run_length, payload)` triples, in write order. A
                None payload is a tile the profile left out, and records
                nothing.

        Returns:
            `(written, skipped, blobs)` for what was just written.
        """
        for tile_id, run_length, output_data in runs:
            if output_data is None:
                continue
            self._tile_ids.append(tile_id)
            self._run_lengths.append(run_length)
            self._run_blobs.append(self._blob_for(output_data))
        return _run_counts(runs)

    def _blob_for(self, output_data):
        """Find a payload's blob, spooling it the first time it is seen.

        Args:
            output_data: The payload.

        Returns:
            The payload's blob index.
        """
        # A run is one object over many tiles, and consecutive entries sharing
        # an `(offset, length)` share one object too: the identity test skips
        # hashing for both.
        if output_data is self._previous_data:
            return self._previous_blob
        digest = hashlib.blake2b(output_data, digest_size=DIGEST_SIZE).digest()
        blob = self._blob_index.get(digest)
        if blob is None:
            blob = len(self._blob_offsets)
            self._blob_index[digest] = blob
            self._blob_offsets.append(self._spool_bytes)
            self._blob_lengths.append(len(output_data))
            self._spool.write(output_data)
            self._spool_bytes += len(output_data)
        self._previous_data = output_data
        self._previous_blob = blob
        return blob

    def finalize(self):
        """Write the part as a clustered PMTiles archive.

        Written to a temporary name and renamed into place, so a worker
        killed before this returns leaves no part at all rather than a short
        one. A part with no tiles is not written either: `pmtiles merge` has
        nothing to take from it.

        Returns:
            True if a part was written.

        Raises:
            ValueError: If two runs cover the same tile, which only a
                partitioning bug can cause.
        """
        try:
            if not self._tile_ids:
                return False
            tile_ids, run_lengths, run_blobs = self._sorted_runs()
            entry_offsets, blob_order = self._layout(run_blobs)
            self._write_archive(tile_ids, run_lengths, run_blobs,
                                entry_offsets, blob_order)
            return True
        finally:
            self._spool.close()

    def _sorted_runs(self):
        """Sort the recorded runs by tile id, and merge neighbours.

        Returns:
            Tile ids, run lengths and blob indices as numpy arrays, sorted by
            tile id, with every two adjacent runs that continue each other
            onto the same blob folded into one.

        Raises:
            ValueError: If two runs cover the same tile.
        """
        tile_ids = np.frombuffer(self._tile_ids, dtype=np.uint64)
        run_lengths = np.frombuffer(self._run_lengths, dtype=np.uint64)
        run_blobs = np.frombuffer(self._run_blobs, dtype=np.uint64)
        # Nearly sorted already: the source archive is clustered, so only the
        # entries it deduplicated arrive out of tile id order.
        order = np.argsort(tile_ids, kind="stable")
        tile_ids = tile_ids[order]
        run_lengths = run_lengths[order]
        run_blobs = run_blobs[order]

        run_ends = tile_ids + run_lengths
        overlapping = np.flatnonzero(tile_ids[1:] < run_ends[:-1])
        if overlapping.size:
            first = int(tile_ids[overlapping[0] + 1])
            raise ValueError(
                f"{self.path}: {overlapping.size} runs overlap the one "
                f"before them, starting at tile {tileid_to_zxy(first)}; a "
                f"tile must come from exactly one manifest entry")

        starts = np.ones(tile_ids.size, dtype=bool)
        starts[1:] = ((tile_ids[1:] != run_ends[:-1])
                      | (run_blobs[1:] != run_blobs[:-1]))
        start_indices = np.flatnonzero(starts)
        return (tile_ids[start_indices],
                np.add.reduceat(run_lengths, start_indices),
                run_blobs[start_indices])

    def _layout(self, run_blobs):
        """Place each blob in the part's tile data section.

        `pmtiles merge` reads every input's tile data front to back while it
        walks the entries in tile id order, so a blob must sit at the point
        where that walk first reaches it: in order of first appearance, with
        nothing between two blobs.

        Args:
            run_blobs: Each run's blob index, in tile id order.

        Returns:
            Each run's offset into the tile data section, and the blob
            indices in the order they are laid out.
        """
        blobs, first_runs = np.unique(run_blobs, return_index=True)
        blob_order = blobs[np.argsort(first_runs)]
        lengths = np.frombuffer(self._blob_lengths, dtype=np.uint64)
        laid_out_lengths = lengths[blob_order]
        blob_offsets = np.zeros(lengths.size, dtype=np.uint64)
        blob_offsets[blob_order] = (np.cumsum(laid_out_lengths)
                                    - laid_out_lengths)
        return blob_offsets[run_blobs], blob_order

    def _write_archive(self, tile_ids, run_lengths, run_blobs, entry_offsets,
                       blob_order):
        """Write header, directories, metadata and tile data, then rename.

        Args:
            tile_ids: Each run's first tile id, sorted.
            run_lengths: Each run's length.
            run_blobs: Each run's blob index.
            entry_offsets: Each run's offset into the tile data section.
            blob_order: The blob indices in the order they are laid out.
        """
        lengths = np.frombuffer(self._blob_lengths, dtype=np.uint64)
        entries = [
            Entry(tile_id, offset, length, run_length)
            for tile_id, offset, length, run_length in zip(
                tile_ids.tolist(), entry_offsets.tolist(),
                lengths[run_blobs].tolist(), run_lengths.tolist())
        ]
        root_bytes, leaves_bytes, unused_leaf_count = optimize_directories(
            entries, ROOT_DIRECTORY_LIMIT)
        metadata_bytes = gzip.compress(
            json.dumps(self.metadata, separators=(",", ":")).encode(), mtime=0)

        min_zoom = tileid_to_zxy(entries[0].tile_id)[0]
        max_zoom = tileid_to_zxy(entries[-1].tile_id)[0]
        metadata_offset = HEADER_LENGTH + len(root_bytes)
        leaf_offset = metadata_offset + len(metadata_bytes)
        tile_data_offset = leaf_offset + len(leaves_bytes)
        header = {
            **WORLD_BOUNDS_E7,
            "root_offset": HEADER_LENGTH,
            "root_length": len(root_bytes),
            "metadata_offset": metadata_offset,
            "metadata_length": len(metadata_bytes),
            "leaf_directory_offset": leaf_offset,
            "leaf_directory_length": len(leaves_bytes),
            "tile_data_offset": tile_data_offset,
            "tile_data_length": int(lengths[blob_order].sum()),
            "addressed_tiles_count": int(run_lengths.sum()),
            "tile_entries_count": len(entries),
            "tile_contents_count": int(blob_order.size),
            "clustered": True,
            "internal_compression": Compression.GZIP,
            "tile_compression": Compression.GZIP,
            "tile_type": TileType.MVT,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "center_zoom": min(max(self.center_zoom, min_zoom), max_zoom),
        }

        temporary_path = self.path + ".tmp"
        with open(temporary_path, "wb") as part:
            part.write(serialize_header(header))
            part.write(root_bytes)
            part.write(metadata_bytes)
            part.write(leaves_bytes)
            self._copy_blobs(part, blob_order)
        os.replace(temporary_path, self.path)

    def _copy_blobs(self, part, blob_order):
        """Copy every blob out of the spool, in layout order.

        Blobs that already sit back to back in the spool, which is most of
        them for a clustered source, go across as one copy.

        Args:
            part: The open part file, positioned at its tile data section.
            blob_order: The blob indices in the order they are laid out.
        """
        self._spool.flush()
        if not self._spool_bytes:
            # Nothing to copy, and mmap refuses an empty file.
            return
        spool_offsets = np.frombuffer(self._blob_offsets,
                                      dtype=np.uint64)[blob_order]
        lengths = np.frombuffer(self._blob_lengths, dtype=np.uint64)[blob_order]
        ends = spool_offsets + lengths
        breaks = np.flatnonzero(spool_offsets[1:] != ends[:-1]) + 1
        span_starts = np.concatenate(([0], breaks))
        span_ends = np.concatenate((breaks, [blob_order.size]))
        with mmap.mmap(self._spool.fileno(), 0,
                       access=mmap.ACCESS_READ) as spool:
            for first, last in zip(spool_offsets[span_starts].tolist(),
                                   ends[span_ends - 1].tolist()):
                for step_start in range(first, last, COPY_STEP_BYTES):
                    part.write(spool[step_start:min(last, step_start
                                                    + COPY_STEP_BYTES)])


def init_part(path, min_zoom, max_zoom, profile, schema, attribution):
    """Start one profile's part, with its metadata.

    Every part carries the layer's complete metadata: `pmtiles merge` copies
    the first input's into the layer and ignores the rest.

    Args:
        path: The part file to write at finalize().
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.
        profile: The profile whose output goes in it.
        schema: The schema the source tiles are in.
        attribution: What the layer credits, from source.json.

    Returns:
        An empty PartWriter.
    """
    metadata = {
        "name": profile.mbtiles_name,
        "format": "pbf",
        "minzoom": str(min_zoom),
        "maxzoom": str(max_zoom),
        "attribution": attribution,
        "vector_layers": profile.vector_layers_json(schema),
    }
    return PartWriter(path, metadata, min_zoom)


def write_gap_tiles(gap_entries, writer, output_data):
    """Write the profile's single gap answer at every gap tile.

    Args:
        gap_entries: The gap entries this worker carries.
        writer: The part to write into.
        output_data: The profile's answer for a gap tile, or None to write
            nothing at all.

    Returns:
        `(written, skipped, blobs)` for what was just written.
    """
    runs = [(entry.tile_id, entry.run_length, output_data)
            for entry in gap_entries]
    written, skipped, blobs = writer.write(runs)
    print(f"gap tiles (no archive entry at all): "
          f"filled {written}, skipped {skipped}", file=sys.stderr)
    return written, skipped, blobs


def finalize_parts(writers):
    """Write every part out.

    Args:
        writers: The parts to finish.
    """
    for writer in writers:
        if not writer.finalize():
            print(f"no tiles written: {writer.path} left out", file=sys.stderr)
