"""Profile contract: one source tile -> one output tile.

See docs/PROFILES.md.
"""
from abc import ABC, abstractmethod

from shapely.geometry.base import BaseGeometry

from tilealchemist import mvt
from tilealchemist.cost import (
    DEFAULT_BYTES_PER_OUTPUT_TILE,
    DEFAULT_SECONDS_PER_TILE,
    DEFAULT_WRITTEN_SHARE,
)
from tilealchemist.features import Feature
from tilealchemist.tile import Tile


class Profile(ABC):
    """What a profile is: one source tile in, one output tile out.

    A profile subclasses this, names itself, and implements `transform()`.
    Everything else here has a default it may override.

    Attributes:
        name: The profile's name, which its output layer and its metadata
            `name` are taken from unless overridden.
        seconds_per_tile: What `transform_tile()` costs on one deduped source
            tile. A profile's own measurement, since the work is its shapely,
            not tilealchemist's; see docs/ARCHITECTURE.md "What a record costs".
        bytes_per_output_tile: What one of this profile's real output tiles
            weighs in the shard. The write is charged on bytes rather than on
            tiles, and how heavy a tile is belongs to the profile that shaped
            it: a coastline profile's tiles are not a label profile's. A gap
            tile is not covered by this; `gap_bytes()` answers for those.
        written_share: The share of the tiles handed to this profile that come
            back with bytes in them. `transform_tile()` returns None wherever
            there is nothing to say and the writer skips those, so a profile
            that speaks for a coastline is silent across an ocean: one planet
            run left 77% of its tiles unwritten. `bytes_per_output_tile` is
            measured over the written ones alone, and this is what scales it
            onto every tile in a record.

    All three figures above are this profile's own *estimate*, used until
    something measures better. None is stored here once a run is under way: the
    caller settles what it will charge and keeps it (`cost.ProfileCost`), so
    nothing can quietly rewrite a profile object mid-run.
    """

    name: str

    seconds_per_tile: float = DEFAULT_SECONDS_PER_TILE

    bytes_per_output_tile: float = DEFAULT_BYTES_PER_OUTPUT_TILE

    written_share: float = DEFAULT_WRITTEN_SHARE

    @property
    def output_layer_name(self):
        """The name this profile's output MVT layer carries."""
        return self.name

    @property
    def mbtiles_name(self):
        """The `name` this profile's output layer carries in its metadata."""
        return self.name

    @abstractmethod
    def transform(self, tile):
        """Turn one source tile into the features the output should carry.

        Args:
            tile: The source tile, decoded.

        Returns:
            A Feature, a geometry, an iterable of either, or None to leave the
            tile out of the output entirely.
        """

    def output_fields(self, schema):
        """The properties this profile's features carry.

        Args:
            schema: The schema the source tiles are in, for a profile passing a
                schema's own fields through to its output.

        Returns:
            A mapping of property name to MVT type, empty by default.
        """
        del schema  # Unused by default.
        return {}

    def vector_layers_json(self, schema):
        """This profile's entry in the output's `vector_layers` metadata.

        Args:
            schema: The schema the source tiles are in.

        Returns:
            The layer descriptions to publish, one of them by default.
        """
        return [{"id": self.output_layer_name,
                 "fields": self.output_fields(schema)}]

    def transform_tile(self, tile):
        """Encode what `transform()` returned into output tile bytes.

        Overridable, for a profile that has to encode its own output.

        Args:
            tile: The source tile, decoded.

        Returns:
            The gzipped output MVT bytes, or None to skip the tile.
        """
        result = self.transform(tile)
        if not result:
            return None
        if isinstance(result, (Feature, BaseGeometry)):
            result = [result]
        features = [item if isinstance(item, Feature) else Feature(item)
                    for item in result]
        return self._encode_tile(features, tile.extent)

    def transform_gap(self, schema):
        """The bytes every gap tile gets.

        Called once per run rather than once per tile: a gap carries no source
        data, so every gap tile comes out the same.

        Args:
            schema: The schema the output is written against.

        Returns:
            The gzipped output MVT bytes, or None to leave gaps out.
        """
        return self.transform_tile(Tile.empty(schema))

    def gap_bytes(self, schema):
        """What one gap tile of this profile's output weighs.

        Not a declared number but a question, because the answer is knowable
        exactly rather than worth estimating: a gap carries no source data, so
        `transform_gap()` gives every gap tile in a run the same bytes. The
        default therefore just measures that, once, and a profile only overrides
        this if it can answer without building the tile.

        Args:
            schema: The schema the output is written against.

        Returns:
            The bytes one gap tile comes to, and zero where this profile leaves
            gaps out entirely.
        """
        data = self.transform_gap(schema)
        return float(len(data)) if data else 0.0

    def _encode_tile(self, features, extent):
        """Encode features into this profile's output layer.

        Args:
            features: The Features to write.
            extent: The tile extent their coordinates are relative to.

        Returns:
            The gzipped output MVT bytes, or None if nothing survived snapping
            onto the output grid.
        """
        return mvt.encode_tile(
            self.output_layer_name,
            [{"geometry": feature.geometry, "properties": feature.properties}
             for feature in features],
            extent)
