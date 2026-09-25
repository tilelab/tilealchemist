"""The vocabulary a profile works in; see docs/PROFILES.md."""
from dataclasses import dataclass, field

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class Feature:
    """One geometry a profile emits, with the properties it carries.

    Attributes:
        geometry: The feature's shapely geometry.
        properties: The feature's properties, empty unless given.
    """

    geometry: BaseGeometry
    properties: dict = field(default_factory=dict)

    def with_geometry(self, geometry):
        """Copy this feature with a different geometry.

        Args:
            geometry: The geometry the copy carries.

        Returns:
            A new Feature holding this feature's properties.
        """
        return Feature(geometry, self.properties)


class FeatureSet:
    """One named kind of source data, identified by object identity."""

    def __init__(self, name, description):
        """Name a feature set.

        Args:
            name: The set's identifier, as logs and errors refer to it.
            description: What a schema is expected to put in it.
        """
        self.name = name
        self.description = description

    def __repr__(self):
        """Render the set as its name.

        Returns:
            A short form such as ``<FeatureSet surface_water>``.
        """
        return f"<FeatureSet {self.name}>"


SURFACE_WATER = FeatureSet(
    "surface_water",
    "Real, non-tunnel surface water polygons: what a renderer would paint as "
    "open water. Water running through a tunnel is not part of this.")

WATERWAYS = FeatureSet(
    "waterways",
    "Waterway line features (rivers, streams, canals) with the schema's own "
    "properties preserved as-is.")
