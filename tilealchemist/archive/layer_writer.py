"""What `convert` writes: one clustered PMTiles archive per profile, a layer.

See docs/reference/cli.md "Layer files".
"""
import array
import hashlib
import mmap
import os
import tempfile
from collections.abc import Iterable
from typing import Any, BinaryIO, TypeAlias

import numpy as np
import numpy.typing as npt
from pmtiles.tile import Compression, Entry, HeaderDict, TileType, tileid_to_zxy
from pmtiles.writer import finalize_header

from tilealchemist.conversion.transform_chunk import TileRun

UInt64Array: TypeAlias = npt.NDArray[np.uint64]

# Dedup digest size: 128 bits can't collide across a planet.
DIGEST_SIZE: int = 16

# Each mmap slice is a copy in RAM, so blobs are copied in steps this big.
COPY_STEP_BYTES: int = 64 * 2 ** 20


class LayerWriter:
    """One profile's layer, collected as runs and laid out at finalize().

    Runs arrive in source offset order, not tile id order, so they are only
    recorded here, each distinct payload spooled to disk once.

    Attributes:
        path: Where finalize() writes the layer.
        metadata: The layer's JSON metadata.
        source_header: The source archive's header, whose bounds and center
            the layer keeps.
    """

    path: str
    metadata: dict[str, Any]
    source_header: HeaderDict
    _spool: BinaryIO
    _spool_bytes: int
    # Quoted: array.array is subscriptable from Python 3.12 on.
    _blob_offsets: "array.array[int]"
    _blob_lengths: "array.array[int]"
    _blob_index: dict[bytes, int]
    _tile_ids: "array.array[int]"
    _run_lengths: "array.array[int]"
    _run_blobs: "array.array[int]"
    _previous_data: bytes | None
    _previous_blob: int

    def __init__(self, path: str, metadata: dict[str, Any],
                 source_header: HeaderDict) -> None:
        self.path = path
        self.metadata = metadata
        self.source_header = source_header
        self._spool = tempfile.TemporaryFile(
            dir=os.path.dirname(os.path.abspath(path)))
        self._spool_bytes = 0
        self._blob_offsets = array.array("Q")
        self._blob_lengths = array.array("Q")
        self._blob_index = {}
        self._tile_ids = array.array("Q")
        self._run_lengths = array.array("Q")
        self._run_blobs = array.array("Q")
        # Held across writes so `is` never meets a reused address; see
        # _blob_for().
        self._previous_data = None
        self._previous_blob = -1

    def write(self, runs: Iterable[TileRun]) -> None:
        """Records runs; those with a None payload are skipped."""
        tile_id: int
        run_length: int
        output_data: bytes | None
        for tile_id, run_length, output_data in runs:
            if output_data is None:
                continue
            self._tile_ids.append(tile_id)
            self._run_lengths.append(run_length)
            self._run_blobs.append(self._blob_for(output_data))

    def _blob_for(self, output_data: bytes) -> int:
        """Returns the payload's blob index, spooling it when new."""
        # Consecutive entries of one source blob share one payload object, so
        # identity skips the hash.
        if output_data is self._previous_data:
            return self._previous_blob
        digest: bytes = hashlib.blake2b(output_data,
                                        digest_size=DIGEST_SIZE).digest()
        blob: int | None = self._blob_index.get(digest)
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

    def finalize(self) -> bool:
        """Writes the layer, atomically; returns False if it has no tiles.

        Raises:
            ValueError: If two runs cover the same tile.
        """
        try:
            if not self._tile_ids:
                return False
            tile_ids: UInt64Array
            run_lengths: UInt64Array
            run_blobs: UInt64Array
            tile_ids, run_lengths, run_blobs = self._sorted_runs()
            entry_offsets: UInt64Array
            blob_order: UInt64Array
            entry_offsets, blob_order = self._layout(run_blobs)
            self._write_archive(tile_ids, run_lengths, run_blobs,
                                entry_offsets, blob_order)
            return True
        finally:
            self._spool.close()

    def _sorted_runs(self) -> tuple[UInt64Array, UInt64Array, UInt64Array]:
        """Returns tile ids, run lengths and blobs sorted, neighbours merged.

        Adjacent runs that continue each other on the same blob become one.

        Raises:
            ValueError: If two runs cover the same tile.
        """
        tile_ids: UInt64Array = np.frombuffer(self._tile_ids, dtype=np.uint64)
        run_lengths: UInt64Array = np.frombuffer(self._run_lengths,
                                                 dtype=np.uint64)
        run_blobs: UInt64Array = np.frombuffer(self._run_blobs,
                                               dtype=np.uint64)
        # Nearly sorted: only deduplicated source entries arrive out of order.
        order: npt.NDArray[np.intp] = np.argsort(tile_ids, kind="stable")
        tile_ids = tile_ids[order]
        run_lengths = run_lengths[order]
        run_blobs = run_blobs[order]

        run_ends: UInt64Array = tile_ids + run_lengths
        overlapping: npt.NDArray[np.intp] = np.flatnonzero(
            tile_ids[1:] < run_ends[:-1])
        if overlapping.size:
            first: int = int(tile_ids[overlapping[0] + 1])
            raise ValueError(
                f"{self.path}: {overlapping.size} runs overlap the one "
                f"before them, starting at tile {tileid_to_zxy(first)}; a "
                f"tile must come from exactly one directory entry")

        # A run starts anew after a hole or on another blob.
        after_hole: npt.NDArray[np.bool_] = tile_ids[1:] != run_ends[:-1]
        new_blob: npt.NDArray[np.bool_] = run_blobs[1:] != run_blobs[:-1]
        starts: npt.NDArray[np.bool_] = np.ones(tile_ids.size, dtype=bool)
        starts[1:] = after_hole | new_blob
        start_indices: npt.NDArray[np.intp] = np.flatnonzero(starts)
        return (tile_ids[start_indices],
                np.add.reduceat(run_lengths, start_indices),
                run_blobs[start_indices])

    def _layout(self, run_blobs: UInt64Array
                ) -> tuple[UInt64Array, UInt64Array]:
        """Places the blobs in the tile data section.

        A clustered archive's tile data follows tile id order, so blobs go in
        order of first use, back to back.

        Args:
            run_blobs: Each run's blob, in tile id order.

        Returns:
            Each run's tile data offset, and the blobs in layout order.
        """
        blobs: UInt64Array
        first_runs: npt.NDArray[np.intp]
        blobs, first_runs = np.unique(run_blobs, return_index=True)
        blob_order: UInt64Array = blobs[np.argsort(first_runs)]
        lengths: UInt64Array = np.frombuffer(self._blob_lengths,
                                             dtype=np.uint64)
        laid_out_lengths: UInt64Array = lengths[blob_order]
        blob_offsets: UInt64Array = np.zeros(lengths.size, dtype=np.uint64)
        blob_offsets[blob_order] = (np.cumsum(laid_out_lengths)
                                    - laid_out_lengths)
        return blob_offsets[run_blobs], blob_order

    def _write_archive(self, tile_ids: UInt64Array, run_lengths: UInt64Array,
                       run_blobs: UInt64Array, entry_offsets: UInt64Array,
                       blob_order: UInt64Array) -> None:
        """Writes header, directories, metadata and tile data, then renames.

        The arrays hold one value per run, in tile id order, except
        `blob_order`, the blobs in layout order.
        """
        lengths: UInt64Array = np.frombuffer(self._blob_lengths,
                                             dtype=np.uint64)
        entries: list[Entry] = [
            Entry(tile_id, offset, length, run_length)
            for tile_id, offset, length, run_length in zip(
                tile_ids.tolist(), entry_offsets.tolist(),
                lengths[run_blobs].tolist(), run_lengths.tolist())
        ]
        min_zoom: int = tileid_to_zxy(entries[0].tile_id)[0]
        max_zoom: int = tileid_to_zxy(entries[-1].tile_id)[0]
        # Not a HeaderDict: finalize_header() fills in the other keys.
        header: dict[str, Any] = {
            "tile_compression": Compression.GZIP,
            "tile_type": TileType.MVT,
            "min_lon_e7": self.source_header["min_lon_e7"],
            "min_lat_e7": self.source_header["min_lat_e7"],
            "max_lon_e7": self.source_header["max_lon_e7"],
            "max_lat_e7": self.source_header["max_lat_e7"],
            # `pmtiles verify` wants it inside the layer's own zoom range.
            "center_zoom": min(max(self.source_header["center_zoom"],
                                   min_zoom), max_zoom),
            "center_lon_e7": self.source_header["center_lon_e7"],
            "center_lat_e7": self.source_header["center_lat_e7"],
        }
        header_bytes: bytes
        root_bytes: bytes
        metadata_bytes: bytes
        leaves_bytes: bytes
        header_bytes, root_bytes, metadata_bytes, leaves_bytes = (
            finalize_header(header,
                            addressed_tiles_count=int(run_lengths.sum()),
                            tile_entries=entries,
                            tile_contents_count=int(blob_order.size),
                            metadata=self.metadata, clustered=True,
                            tile_data_length=int(lengths[blob_order].sum())))

        temporary_path: str = self.path + ".tmp"
        layer: BinaryIO
        with open(temporary_path, "wb") as layer:
            layer.write(header_bytes)
            layer.write(root_bytes)
            layer.write(metadata_bytes)
            layer.write(leaves_bytes)
            self._copy_blobs(layer, blob_order)
        os.replace(temporary_path, self.path)

    def _copy_blobs(self, layer: BinaryIO, blob_order: UInt64Array) -> None:
        """Copies the blobs from the spool into `layer`, in layout order.

        Blobs already back to back in the spool, most of them for a clustered
        source, are copied as one span.
        """
        self._spool.flush()
        if not self._spool_bytes:
            # mmap refuses an empty file.
            return
        spool_offsets: UInt64Array = np.frombuffer(
            self._blob_offsets, dtype=np.uint64)[blob_order]
        lengths: UInt64Array = np.frombuffer(self._blob_lengths,
                                             dtype=np.uint64)[blob_order]
        ends: UInt64Array = spool_offsets + lengths
        breaks: npt.NDArray[np.intp] = np.flatnonzero(
            spool_offsets[1:] != ends[:-1]) + 1
        span_starts: npt.NDArray[np.intp] = np.concatenate(([0], breaks))
        span_ends: npt.NDArray[np.intp] = np.concatenate(
            (breaks, [blob_order.size]))
        spool: mmap.mmap
        first: int
        last: int
        step_start: int
        with mmap.mmap(self._spool.fileno(), 0,
                       access=mmap.ACCESS_READ) as spool:
            for first, last in zip(spool_offsets[span_starts].tolist(),
                                   ends[span_ends - 1].tolist()):
                for step_start in range(first, last, COPY_STEP_BYTES):
                    layer.write(spool[step_start:min(last, step_start
                                                     + COPY_STEP_BYTES)])
