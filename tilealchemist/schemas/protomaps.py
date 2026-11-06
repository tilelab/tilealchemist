"""The Protomaps basemap schema."""
from tilealchemist.profiles.features import SURFACE_WATER, WATERWAYS
from tilealchemist.profiles.mvt import DecodedLayers
from tilealchemist.schemas.base import (
    DecodedFeature,
    TileSchema,
    feature,
    layer_features,
)

# Protomaps' `water` layer holds polygons, lines and label points.
POLYGON_TYPES: frozenset[str] = frozenset({"Polygon", "MultiPolygon"})
LINE_TYPES: frozenset[str] = frozenset({"LineString", "MultiLineString"})


class ProtomapsSchema(TileSchema):
    """Protomaps basemap tiles."""

    name: str = "protomaps"
    # OpenMapTiles has `water` too, but no `earth`.
    archive_layers: frozenset[str] = frozenset({"water", "earth"})
    # Water polygons reach the full 8 pixels.
    buffer_pixels: int = 8
    default_extent: int = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the `water` layer's polygons without tunnels."""
        # Every `kind`: Protomaps' styles paint them all as water. `tunnel`
        # is only set from z14.
        return [
            polygon
            for polygon in layer_features(layers, "water")
            if polygon["geometry"]["type"] in POLYGON_TYPES
            and polygon["properties"].get("tunnel", "no") == "no"
        ]

    @feature(WATERWAYS, fields={"kind": "String", "name": "String",
                                "layer": "Number", "min_zoom": "Number",
                                "sort_rank": "Number"})
    def waterways(self, layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the `water` layer's lines."""
        return [line for line in layer_features(layers, "water")
                if line["geometry"]["type"] in LINE_TYPES]


PROTOMAPS: ProtomapsSchema = ProtomapsSchema()
