"""Profile contract: one source tile -> one output tile; see docs/PROFILES.md."""
from abc import ABC, abstractmethod

from shapely.geometry.base import BaseGeometry

from tilealchemist import mvt
from tilealchemist.features import Feature
from tilealchemist.tile import Tile


class Profile(ABC):
    """What a profile is: one source tile in, one output tile out.

    A profile subclasses this, names itself, and implements `transform()`.
    Everything else here has a default it may override.

    Attributes:
        name: The profile's name, which its output layer and its .mbtiles are
            named after unless overridden.
    """

    name: str

    @property
    def output_layer_name(self):
        """The name this profile's output MVT layer carries."""
        return self.name

    @property
    def mbtiles_name(self):
        """The name this profile's output .mbtiles file carries."""
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
        return {}

    def vector_layers_json(self, schema):
        """This profile's entry in the output archive's `vector_layers` metadata.

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
