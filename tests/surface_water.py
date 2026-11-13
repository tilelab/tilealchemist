"""Test profile: each tile's surface water, unchanged."""
from shapely.geometry.base import BaseGeometry

from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.features import SURFACE_WATER
from tilealchemist.profiles.tile import Tile


class SurfaceWaterProfile(Profile):
    """The schema's surface water polygons."""

    name: str = "surface-water"

    def transform(self, tile: Tile) -> list[BaseGeometry]:
        return [each.geometry for each in tile.features(SURFACE_WATER)]


PROFILE: type[Profile] = SurfaceWaterProfile
