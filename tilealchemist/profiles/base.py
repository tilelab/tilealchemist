"""The Profile contract: one source tile in, one output tile out.

See docs/reference/profile-api.md.
"""
from abc import ABC, abstractmethod
from typing import Any

from tilealchemist.profiles import mvt
from tilealchemist.profiles.features import TransformResult
from tilealchemist.profiles.tile import Tile
from tilealchemist.schemas.base import TileSchema


class Profile(ABC):
    """Turns each source tile into one output tile.

    Subclasses set `name` and implement `transform()`; everything else has a
    default.

    Attributes:
        name: The profile's name, which its layer is named after.
    """

    name: str

    @property
    def layer_name(self) -> str:
        """The output MVT layer's name."""
        return self.name

    def layer_fields(self, schema: TileSchema) -> dict[str, str]:
        """Returns property name to MVT type of the output; empty by default.

        Args:
            schema: The source's schema, for profiles passing its fields on.
        """
        del schema  # Unused by default.
        return {}

    def vector_layers(self, schema: TileSchema) -> list[dict[str, Any]]:
        """Returns the output's `vector_layers` metadata."""
        return [{"id": self.layer_name, "fields": self.layer_fields(schema)}]

    @abstractmethod
    def transform(self, tile: Tile) -> TransformResult:
        """Returns what the output tile holds for `tile`.

        Returns:
            A Feature, a geometry, an iterable of either, or None to leave
            the tile out.
        """

    def build_tile(self, tile: Tile) -> bytes | None:
        """Returns `transform(tile)` encoded, or None to leave it out.

        Override to encode the output yourself, such as several MVT layers
        with `mvt.encode_tile()`.
        """
        return mvt.encode_tile({self.layer_name: self.transform(tile)},
                               tile.extent)
