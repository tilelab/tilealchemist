"""Gzipped MVT decoding and encoding, snapped to the output grid."""
import gzip
from collections.abc import Iterable, Mapping
from typing import Any, TypeAlias

import mapbox_vector_tile
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_raise
from shapely.geometry.base import BaseGeometry

from tilealchemist.profiles.features import Feature, TransformResult

# mapbox_vector_tile.decode()'s result: layer name to layer.
DecodedLayers: TypeAlias = dict[str, dict[str, Any]]

# MVT coordinates are integers.
OUTPUT_GRID_SIZE: float = 1.0


def decode_tile(data: bytes) -> DecodedLayers:
    """Returns the layers of a gzipped MVT tile."""
    layers: DecodedLayers = mapbox_vector_tile.decode(gzip.decompress(data))
    return layers


def snap_to_output_grid(geometry: BaseGeometry) -> BaseGeometry:
    """Returns `geometry` snapped validly onto the integer grid.

    The encoder rounds anyway, but not topology-aware: parts 0.4 units apart
    would become a self-intersecting multipolygon, which aborts the run, and
    features narrower than a unit empty rings or layers. The result is empty
    if the geometry collapsed.
    """
    snapped: BaseGeometry = shapely.set_precision(
        geometry, OUTPUT_GRID_SIZE, mode="valid_output")
    return snapped


def _features(result: TransformResult) -> list[Feature]:
    """Returns what `transform()` returned as Features."""
    if result is None:
        return []
    items: Iterable[Feature | BaseGeometry] = (
        [result] if isinstance(result, (Feature, BaseGeometry)) else result)
    return [item if isinstance(item, Feature) else Feature(item)
            for item in items]


def encode_tile(layers: Mapping[str, TransformResult],
                extent: int) -> bytes | None:
    """Encodes MVT layers as one gzipped MVT tile.

    Geometries are snapped first; those that collapse are dropped, and so
    are layers left without any.

    Args:
        layers: Each MVT layer's name and contents, as `transform()` returns
            them.
        extent: The extent the coordinates are relative to.

    Returns:
        The tile bytes, or None if no layer kept a geometry.
    """
    encoded_layers: list[dict[str, Any]] = []
    name: str
    result: TransformResult
    for name, result in layers.items():
        snapped: list[dict[str, Any]] = []
        feature: Feature
        for feature in _features(result):
            geometry: BaseGeometry = snap_to_output_grid(feature.geometry)
            if not geometry.is_empty:
                snapped.append({"geometry": geometry,
                                "properties": feature.properties})
        if snapped:
            encoded_layers.append({"name": name, "features": snapped})
    if not encoded_layers:
        return None
    encoded: bytes = mapbox_vector_tile.encode(
        encoded_layers,
        default_options={"extents": extent,
                         "on_invalid_geometry": on_invalid_geometry_raise},
    )
    # mtime=0 keeps identical tiles byte-identical, so PMTiles dedups them.
    return gzip.compress(encoded, mtime=0)
