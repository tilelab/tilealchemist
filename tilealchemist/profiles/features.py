"""What a profile reads and emits; see docs/reference/profile-api.md."""
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class Feature:
    """A geometry and its properties."""

    geometry: BaseGeometry
    properties: dict[str, Any] = field(default_factory=dict)

    def with_geometry(self, geometry: BaseGeometry) -> "Feature":
        """Returns a copy with another geometry and the same properties."""
        return Feature(geometry, self.properties)


# What Profile.transform() returns for an MVT layer; None leaves it out.
TransformResult: TypeAlias = (Feature | BaseGeometry
                              | Iterable[Feature | BaseGeometry] | None)


class FeatureSet:
    """A named kind of source data a schema provides, compared by identity.

    Attributes:
        name: Identifier used in logs and errors.
        description: What a schema is expected to put in it.
    """

    name: str
    description: str

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def __repr__(self) -> str:
        return f"<FeatureSet {self.name}>"


SURFACE_WATER: FeatureSet = FeatureSet(
    "surface_water",
    "Surface water polygons a renderer would paint as open water, without "
    "water in tunnels.")

WATERWAYS: FeatureSet = FeatureSet(
    "waterways",
    "Waterway lines (rivers, streams, canals) with the schema's own "
    "properties.")
