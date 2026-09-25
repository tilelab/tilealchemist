"""Tile schema contract, as behavior a profile calls; see docs/PROFILES.md."""
from abc import ABC
from enum import StrEnum

from tilealchemist.features import SURFACE_WATER, WATERWAYS


class SchemaName(StrEnum):
    """The schemas this build knows, by the name a CLI flag uses."""

    OPENMAPTILES = "openmaptiles"
    PROTOMAPS = "protomaps"


def feature(feature_set, *, fields=None):
    """Mark a TileSchema method as answering one feature set.

    Args:
        feature_set: The FeatureSet the method returns, in raw decoded form.
        fields: The properties that set's features carry, as a mapping of name
            to MVT type, for a profile writing them through to its output.

    Returns:
        The decorator that marks the method.
    """
    def mark(method):
        method.feature_set = feature_set
        method.feature_fields = fields or {}
        return method
    return mark


def _layer_features(layers, layer_name):
    """Read one layer's features out of a decoded tile.

    Args:
        layers: The decoded layers, in decode_tile()'s shape.
        layer_name: The layer to read.

    Returns:
        That layer's features, empty where the tile has no such layer.
    """
    layer = layers.get(layer_name)
    if not layer:
        return []
    return layer["features"]


class TileSchema(ABC):
    """What a profile asks of its tiles, whatever encoded them.

    A subclass answers feature sets by marking methods with `@feature`, which
    `__init_subclass__` collects into `provides`. A profile then asks for a
    set rather than for a layer, and the same profile runs against every
    schema that answers it.

    Attributes:
        name: Which schema this is.
        default_buffer_pixels: How far features run past the tile edge.
        default_extent: The coordinate extent a tile is encoded against.
        tile_size_pixels: The tile's rendered size, which the buffer is in
            terms of.
        provides: The feature sets this schema answers, each mapped to the
            name of the method that answers it.
    """

    name: SchemaName
    default_buffer_pixels: int
    default_extent: int
    tile_size_pixels: int = 256
    provides = {}

    def __init_subclass__(cls, **kwargs):
        """Collect the subclass's `@feature` methods into `provides`.

        Args:
            **kwargs: Passed through to the base implementation.
        """
        super().__init_subclass__(**kwargs)
        cls.provides = {
            method.feature_set: attribute_name
            for attribute_name in dir(cls)
            if (method := getattr(cls, attribute_name, None)) is not None
            and hasattr(method, "feature_set")
        }

    def __repr__(self):
        """Render the schema as its name.

        Returns:
            A short form such as ``<TileSchema openmaptiles>``.
        """
        return f"<TileSchema {self.name}>"

    def extract(self, feature_set, layers):
        """Pull one feature set out of a decoded tile.

        Args:
            feature_set: The FeatureSet to answer.
            layers: The decoded layers, in decode_tile()'s shape.

        Returns:
            That set's features, in raw decoded form.

        Raises:
            KeyError: If this schema answers no such feature set.
        """
        return self._method_for(feature_set)(layers)

    def fields_for(self, feature_set):
        """The properties a feature set's features carry.

        Args:
            feature_set: The FeatureSet to describe.

        Returns:
            A mapping of property name to MVT type, empty where the set declares
            none.

        Raises:
            KeyError: If this schema answers no such feature set.
        """
        return self._method_for(feature_set).feature_fields

    def _method_for(self, feature_set):
        """Find the method answering a feature set.

        Args:
            feature_set: The FeatureSet to answer.

        Returns:
            The bound method that answers it.

        Raises:
            KeyError: If this schema answers no such feature set, naming the ones
                it does answer.
        """
        try:
            name = self.provides[feature_set]
        except KeyError:
            available = ", ".join(sorted(each.name for each in self.provides))
            raise KeyError(
                f"schema '{self.name}' provides no feature set "
                f"{feature_set.name!r} (it provides: {available})") from None
        return getattr(self, name)


class OpenMapTilesSchema(TileSchema):
    """OpenMapTiles tiles, as OpenFreeMap's Planetiler builds publish them."""

    name = SchemaName.OPENMAPTILES
    # Planetiler's default, which OpenMapTiles leaves alone for water/waterway.
    default_buffer_pixels = 4
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        """The water polygons a renderer would paint as open water.

        Args:
            layers: The decoded layers, in decode_tile()'s shape.

        Returns:
            The `water` layer's features, less the ones running through a tunnel.
        """
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["properties"].get("brunnel") != "tunnel"
        ]

    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers):
        """The waterway lines, with the schema's own properties left as they are.

        Args:
            layers: The decoded layers, in decode_tile()'s shape.

        Returns:
            The `waterway` layer's features.
        """
        return _layer_features(layers, "waterway")


# Protomaps puts polygons, lines and label points in one `water` layer.
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
LINE_TYPES = {"LineString", "MultiLineString"}


class ProtomapsSchema(TileSchema):
    """Protomaps basemap tiles, whose one `water` layer holds several kinds."""

    name = SchemaName.PROTOMAPS
    # The wider of the schema's two, since water polygons reach the full 8.
    default_buffer_pixels = 8
    default_extent = 4096

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        """The water polygons a renderer would paint as open water.

        Args:
            layers: The decoded layers, in decode_tile()'s shape.

        Returns:
            The polygons in the `water` layer, less the ones in a tunnel. Lines
            and label points share that layer, and are left out.
        """
        return [
            polygon
            for polygon in _layer_features(layers, "water")
            if polygon["geometry"]["type"] in POLYGON_TYPES
            and polygon["properties"].get("tunnel", "no") == "no"
        ]

    @feature(WATERWAYS, fields={"kind": "String", "name": "String",
                                "layer": "Number", "min_zoom": "Number",
                                "sort_rank": "Number"})
    def waterways(self, layers):
        """The waterway lines, with the schema's own properties left as they are.

        Args:
            layers: The decoded layers, in decode_tile()'s shape.

        Returns:
            The lines in the `water` layer, which also holds the polygons and the
            label points.
        """
        return [line for line in _layer_features(layers, "water")
                if line["geometry"]["type"] in LINE_TYPES]


OPENMAPTILES = OpenMapTilesSchema()
PROTOMAPS = ProtomapsSchema()

SCHEMAS = {schema.name: schema for schema in (OPENMAPTILES, PROTOMAPS)}
