"""Water polygon math for profiles.

Profiles cutting against the same union meet without seams. The cut runs at
float precision: `grid_size=` once hid an invalid union (OpenFreeMap
20260906_080001, tile 9/269/151) and cost 2.7x the time. Lines aren't
snapped first: across ~37,600 waterway lines no cut failed without it, and
with it lines and land disagreed by up to half a unit about where water
ends.
"""
import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from tilealchemist.profiles.features import SURFACE_WATER
from tilealchemist.profiles.mvt import OUTPUT_GRID_SIZE
from tilealchemist.profiles.tile import Tile

# Half an output cell: closes gaps that would snap into slivers of land.
# Coastlines shrink by 1/32 px.
WATER_GAP_CLOSING_BUFFER: float = OUTPUT_GRID_SIZE / 2


def surface_water_union(tile: Tile) -> BaseGeometry | None:
    """Returns the tile's surface water as one closed polygon, or None."""
    return tile.derived("water", "surface_water_union", _compute_union)


def _compute_union(tile: Tile) -> BaseGeometry | None:
    """Returns the repaired, gap-closed union of the tile's surface water."""
    polygons: list[BaseGeometry] = [
        feature.geometry.buffer(0) for feature in tile.features(SURFACE_WATER)]
    if not polygons:
        return None
    # The buffer can pinch off a skerry as an overlapping duplicate shell:
    # "structure" merges it, "linework" would leave a phantom island.
    union: BaseGeometry = shapely.make_valid(
        unary_union(polygons).buffer(WATER_GAP_CLOSING_BUFFER),
        method="structure", keep_collapsed=False)
    return union


def subtract_water(tile: Tile, geometry: BaseGeometry) -> BaseGeometry:
    """Returns `geometry` minus the tile's surface water."""
    union: BaseGeometry | None = surface_water_union(tile)
    if union is None:
        return geometry
    return geometry.difference(union)
