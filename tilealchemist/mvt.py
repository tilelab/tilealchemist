"""Thin MVT codec, and the output grid every encoded tile is snapped onto."""
import gzip

import mapbox_vector_tile
import shapely
from mapbox_vector_tile.encoder import on_invalid_geometry_raise

# One unit: MVT coordinates are integers relative to a tile's extent, by spec.
OUTPUT_GRID_SIZE = 1.0


def decode_tile(data):
    """Decode a gzipped MVT tile.

    Args:
        data: The tile's bytes, as the archive stores them.

    Returns:
        The decoded layers, in mapbox_vector_tile's own shape.
    """
    return mapbox_vector_tile.decode(gzip.decompress(data))


def snap_to_output_grid(geometry):
    """Move a geometry onto the integer grid a tile is encoded against.

    Topology-aware, and not an optimization: the encoder rounds either way,
    and doing it here is what keeps the rounded result valid.

    Args:
        geometry: The geometry to snap.

    Returns:
        The snapped geometry, which is empty if it collapsed.
    """
    return shapely.set_precision(geometry, OUTPUT_GRID_SIZE, mode="valid_output")


def encode_tile(layer_name, features, extent):
    """Encode features into one gzipped MVT layer.

    Args:
        layer_name: The name the output layer carries.
        features: Mappings with a "geometry" key, plus whatever properties the
            encoder should write alongside it.
        extent: The tile extent the coordinates are relative to.

    Returns:
        The gzipped tile bytes, or None if every geometry collapsed to empty
        when snapped onto the output grid.
    """
    # Must precede encoding; docs/PROFILES.md "The output grid" says what breaks otherwise.
    snapped = []
    for feature in features:
        geometry = snap_to_output_grid(feature["geometry"])
        if geometry.is_empty:
            continue
        snapped.append({**feature, "geometry": geometry})
    if not snapped:
        return None
    encoded = mapbox_vector_tile.encode(
        {"name": layer_name, "features": snapped},
        default_options={"extents": extent, "on_invalid_geometry": on_invalid_geometry_raise},
    )
    # mtime=0 keeps identical tiles byte-identical across workers, for PMTiles dedup.
    return gzip.compress(encoded, mtime=0)
