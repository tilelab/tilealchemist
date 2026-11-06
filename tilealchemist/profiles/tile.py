"""A source tile as a profile sees it; see docs/reference/profile-api.md."""
from collections.abc import Callable, Hashable
from functools import cached_property
from typing import Any, Self, TypeVar, cast

from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from tilealchemist.profiles import mvt
from tilealchemist.profiles.features import Feature, FeatureSet
from tilealchemist.schemas.base import TileSchema

_Value = TypeVar("_Value")

# Tile generators such as Planetiler and Tippecanoe state buffers in pixels
# of a 256-pixel tile, whatever size a renderer draws it at.
BUFFER_TILE_SIZE_PIXELS: int = 256


class Tile:
    """A decoded source tile.

    Derived values are computed on first use and kept, so profiles sharing
    a tile compute them once.

    Attributes:
        layers: The decoded layers.
        schema: The schema they are encoded in.
    """

    layers: mvt.DecodedLayers
    schema: TileSchema
    _derived: dict[tuple[str, Hashable], Any]

    def __init__(self, layers: mvt.DecodedLayers, schema: TileSchema) -> None:
        self.layers = layers
        self.schema = schema
        self._derived = {}

    @classmethod
    def decode(cls, data: bytes, schema: TileSchema) -> Self:
        """Returns the tile encoded in `data`, gzipped MVT."""
        return cls(mvt.decode_tile(data), schema)

    @cached_property
    def extent(self) -> int:
        """The extent the tile's coordinates are relative to."""
        if not self.layers:
            return self.schema.default_extent
        # MVT allows one per layer; real tiles use one for all.
        extent: int = next(iter(self.layers.values()))["extent"]
        return extent

    @cached_property
    def buffered_square(self) -> BaseGeometry:
        """The tile's square, grown by the schema's buffer."""
        buffer: float = (self.extent * self.schema.buffer_pixels
                         / BUFFER_TILE_SIZE_PIXELS)
        return box(-buffer, -buffer, self.extent + buffer, self.extent + buffer)

    def features(self, feature_set: FeatureSet) -> list[Feature]:
        """Returns the tile's features of `feature_set`."""
        return self.derived("features", feature_set, lambda tile: [
            Feature(shape(feature["geometry"]), feature["properties"])
            for feature in tile.schema.extract(feature_set, tile.layers)
        ])

    def derived(self, namespace: str, key: Hashable,
                compute: Callable[["Tile"], _Value]) -> _Value:
        """Returns `compute(self)`, memoized under `(namespace, key)`."""
        memo_key: tuple[str, Hashable] = (namespace, key)
        if memo_key not in self._derived:
            self._derived[memo_key] = compute(self)
        return cast(_Value, self._derived[memo_key])
