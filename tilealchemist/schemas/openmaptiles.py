"""OpenMapTiles, OpenFreeMap's schema."""
from tilealchemist.profiles.features import SURFACE_WATER, WATERWAYS
from tilealchemist.profiles.mvt import DecodedLayers
from tilealchemist.schemas.base import (
    DecodedFeature,
    TileSchema,
    feature,
    layer_features,
)


class OpenMapTilesSchema(TileSchema):
    """OpenMapTiles as OpenFreeMap's Planetiler builds publish it."""

    name: str = "openmaptiles"
    # Protomaps has no `waterway`.
    archive_layers: frozenset[str] = frozenset({"water", "waterway"})
    # Planetiler's default, kept by OpenMapTiles for water and waterway.
    buffer_pixels: int = 4
    default_extent: int = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the `water` layer without tunnels."""
        return [
            polygon
            for polygon in layer_features(layers, "water")
            if polygon["properties"].get("brunnel") != "tunnel"
        ]

    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the `waterway` layer."""
        return layer_features(layers, "waterway")


OPENMAPTILES: OpenMapTilesSchema = OpenMapTilesSchema()
