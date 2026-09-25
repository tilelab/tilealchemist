"""Shared water-polygon geometry math; see docs/PROFILES.md "Subtracting water"."""
import shapely
from shapely.ops import unary_union

from tilealchemist.features import SURFACE_WATER
from tilealchemist.mvt import OUTPUT_GRID_SIZE

# Half an output cell, closing sub-unit gaps that would snap into slivers of land.
WATER_GAP_CLOSING_BUFFER = OUTPUT_GRID_SIZE / 2


def surface_water_union(tile):
    """Closed union of `tile`'s SURFACE_WATER polygons, None if it has none."""
    return tile.derived("water", "surface_water_union", _compute_union)


def _compute_union(tile):
    """Union a tile's surface water into one closed polygon.

    Each polygon is repaired before the union, and the result is buffered by
    half an output cell, so that sub-unit gaps do not survive snapping as
    slivers of land.

    Args:
        tile: The tile whose SURFACE_WATER features to merge.

    Returns:
        The merged water polygon, or None if the tile carries no water.
    """
    polygons = [feature.geometry.buffer(0)
                for feature in tile.features(SURFACE_WATER)]
    if not polygons:
        return None
    # Repairs what the buffer pinched off; "structure" merges the overlap, "linework" splits it.
    return shapely.make_valid(
        unary_union(polygons).buffer(WATER_GAP_CLOSING_BUFFER),
        method="structure", keep_collapsed=False)


def subtract_water(tile, geometry):
    """`geometry` minus `tile`'s surface water; an operation so every profile cuts alike."""
    union = surface_water_union(tile)
    if union is None:
        return geometry
    return geometry.difference(union)
