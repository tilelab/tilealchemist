"""Builds a layer from a generated archive and checks what `convert` wrote.

    python tests/smoke_test.py

The archive is made up, so the run needs no network, and the profile
passes water through unchanged, so a failure is `convert`'s.
"""
import gzip
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from typing import Any, BinaryIO

import mapbox_vector_tile
from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import Compression, HeaderDict, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from tilealchemist.profiles.mvt import decode_tile

TESTS_DIR: str = os.path.dirname(os.path.abspath(__file__))

# layers.toml's max_zoom.
MAX_ZOOM: int = 3
EXTENT: int = 4096
ATTRIBUTION: str = "&copy; Test"

# A lake in every tile of an even column: the layer leaves the others out
# and stores the lake tiles once.
LAKE: BaseGeometry = box(1024, 1024, 3072, 3072)


def _has_lake(x: int) -> bool:
    """Returns whether tiles of column `x` hold the lake."""
    return x % 2 == 0


def _encode_tile(lake: bool) -> bytes:
    """Returns a gzipped Protomaps tile: land, and the lake if `lake`."""
    layers: list[dict[str, Any]] = [{"name": "earth", "features": [
        {"geometry": box(0, 0, EXTENT, EXTENT), "properties": {}}]}]
    if lake:
        layers.append({"name": "water", "features": [
            {"geometry": LAKE, "properties": {"kind": "lake"}}]})
    encoded: bytes = mapbox_vector_tile.encode(
        layers, default_options={"extents": EXTENT})
    return gzip.compress(encoded, mtime=0)


def _write_archive(path: str) -> int:
    """Writes a Protomaps archive of zoom levels 0 to MAX_ZOOM.

    Returns:
        The number of tiles holding the lake.
    """
    tiles: list[tuple[int, bool]] = sorted(
        (zxy_to_tileid(z, x, y), _has_lake(x))
        for z in range(MAX_ZOOM + 1)
        for x in range(2 ** z)
        for y in range(2 ** z))
    header: dict[str, Any] = {
        "tile_type": TileType.MVT,
        "tile_compression": Compression.GZIP,
        "min_lon_e7": -1800000000, "min_lat_e7": -850511287,
        "max_lon_e7": 1800000000, "max_lat_e7": 850511287,
        "center_zoom": 0, "center_lon_e7": 0, "center_lat_e7": 0,
    }
    metadata: dict[str, Any] = {
        "attribution": ATTRIBUTION,
        "vector_layers": [{"id": "earth", "fields": {}},
                          {"id": "water", "fields": {"kind": "String"}}],
    }
    file: BinaryIO
    with open(path, "wb") as file:
        writer: Writer = Writer(file)
        tile_id: int
        lake: bool
        for tile_id, lake in tiles:
            writer.write_tile(tile_id, _encode_tile(lake))
        writer.finalize(header, metadata)
    return sum(lake for _, lake in tiles)


def _check_layer(path: str, lake_tiles: int) -> None:
    """Checks the layer `convert` wrote to `path`."""
    file: BinaryIO
    with open(path, "rb") as file:
        reader: Reader = Reader(MmapSource(file))
        header: HeaderDict = reader.header()
        metadata: dict[str, Any] = reader.metadata()
        tiles: Iterator[tuple[tuple[int, int, int], bytes]] = all_tiles(
            reader.get_bytes)
        zxy: tuple[int, int, int]
        data: bytes
        for zxy, data in tiles:
            assert _has_lake(zxy[1]), f"tile {zxy} has no lake to keep"
            layer: dict[str, Any] = decode_tile(data)["surface-water"]
            geometry: BaseGeometry = shape(layer["features"][0]["geometry"])
            assert geometry.equals(LAKE), f"tile {zxy} holds {geometry}"
    assert header["clustered"], "the layer isn't clustered"
    assert header["addressed_tiles_count"] == lake_tiles, (
        f"{header['addressed_tiles_count']} tiles, not {lake_tiles}")
    assert header["tile_contents_count"] == 1, (
        f"the lake tiles are stored {header['tile_contents_count']} times")
    assert metadata["name"] == "surface-water", metadata
    assert metadata["attribution"] == ATTRIBUTION, metadata
    assert metadata["vector_layers"] == [
        {"id": "surface-water", "fields": {}}], metadata


def main() -> None:
    """Runs the test in a temporary directory."""
    work_dir: str
    with tempfile.TemporaryDirectory() as work_dir:
        archive: str = os.path.join(work_dir, "archive.pmtiles")
        lake_tiles: int = _write_archive(archive)
        # Two processes, so chunks go through the pool.
        subprocess.run(
            [sys.executable, "-m", "tilealchemist", "convert", archive,
             os.path.join(TESTS_DIR, "layers.toml"), "-o", work_dir,
             "--transform-processes", "2"],
            check=True)
        _check_layer(os.path.join(work_dir, "surface-water.pmtiles"),
                     lake_tiles)
    print("smoke test passed")


if __name__ == "__main__":
    main()
