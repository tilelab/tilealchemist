"""One source tile as a profile sees it; see docs/PROFILES.md."""
from functools import cached_property

from shapely.geometry import box, shape

from tilealchemist import mvt
from tilealchemist.features import Feature


class Tile:
    """One source tile, as a profile sees it.

    Decoded features and every derived value are computed on first use and
    kept, so that several profiles walking the same tile pay for them once
    between them.

    Attributes:
        layers: The decoded layers, in decode_tile()'s shape.
        schema: The schema those layers are encoded in.
    """

    def __init__(self, layers, schema):
        """Wrap decoded layers as a tile.

        Args:
            layers: The decoded layers, in decode_tile()'s shape.
            schema: The schema those layers are encoded in.
        """
        self.layers = layers  # decode_tile()'s {layer_name: {...}} dict.
        self.schema = schema
        self._derived = {}

    @classmethod
    def decode(cls, data, schema):
        """Decode a tile from its stored bytes.

        Args:
            data: The tile's gzipped MVT bytes.
            schema: The schema it is encoded in.

        Returns:
            The decoded Tile.
        """
        return cls(mvt.decode_tile(data), schema)

    @classmethod
    def empty(cls, schema):
        """A tile with no layers, for a gap the archive holds nothing for.

        Args:
            schema: The schema the output is written against.

        Returns:
            An empty Tile.
        """
        return cls({}, schema)

    @cached_property
    def extent(self):
        """The coordinate extent this tile's geometry is relative to."""
        if not self.layers:
            return self.schema.default_extent
        # MVT allows an extent per layer; a real tile encodes every layer at one.
        return next(iter(self.layers.values()))["extent"]

    @cached_property
    def buffered_square(self):
        """The tile's square, grown by the schema's buffer."""
        buffer = self.extent * self.schema.default_buffer_pixels / self.schema.tile_size_pixels
        return box(-buffer, -buffer, self.extent + buffer, self.extent + buffer)

    def features(self, feature_set):
        """This tile's `Feature`s for `feature_set`, computed once however many profiles ask."""
        return self.derived("features", feature_set, lambda tile: [
            Feature(shape(feature["geometry"]), feature["properties"])
            for feature in tile.schema.extract(feature_set, tile.layers)
        ])

    def derived(self, namespace, key, compute):
        """`compute(self)`, memoized on this tile under the owning helper's `namespace`."""
        memo_key = (namespace, key)
        if memo_key not in self._derived:
            self._derived[memo_key] = compute(self)
        return self._derived[memo_key]
