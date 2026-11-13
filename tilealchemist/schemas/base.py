"""The TileSchema contract; see docs/reference/schemas.md."""
from abc import ABC
from collections.abc import Callable
from typing import Any, ClassVar, Protocol, TypeAlias, TypeVar

from tilealchemist.profiles.features import FeatureSet
from tilealchemist.profiles.mvt import DecodedLayers

# One feature as mapbox_vector_tile decodes it.
DecodedFeature: TypeAlias = dict[str, Any]

_Method = TypeVar("_Method", bound=Callable[..., list[DecodedFeature]])


class _FeatureMethod(Protocol):
    """A bound `@feature` method."""

    feature_set: FeatureSet
    feature_fields: dict[str, str]

    def __call__(self, layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the feature set's features in `layers`."""


def feature(feature_set: FeatureSet, *, fields: dict[str, str] | None = None
            ) -> Callable[[_Method], _Method]:
    """Marks a TileSchema method as the one returning `feature_set`.

    Args:
        feature_set: The FeatureSet the method returns.
        fields: Property name to MVT type, for profiles that pass the
            properties through.
    """
    def mark(method: _Method) -> _Method:
        setattr(method, "feature_set", feature_set)
        setattr(method, "feature_fields", fields or {})
        return method
    return mark


def layer_features(layers: DecodedLayers,
                   layer_name: str) -> list[DecodedFeature]:
    """Returns a layer's features, empty if the tile lacks the layer."""
    layer: dict[str, Any] | None = layers.get(layer_name)
    if not layer:
        return []
    features: list[DecodedFeature] = layer["features"]
    return features


class TileSchema(ABC):
    """The feature sets a tile encoding provides.

    Subclasses mark methods with `@feature`; profiles ask for feature sets,
    not layers, so one profile runs on every schema providing them.

    Attributes:
        name: The name the schema is installed under, as configs name it.
        archive_layers: Layers every archive in the schema lists in its
            metadata's `vector_layers`: those it reads, and enough to tell
            it from other schemas. `convert` refuses an archive lacking one.
        buffer_pixels: How far features reach past the tile edge, in
            pixels of a 256-pixel tile.
        default_extent: The extent of a tile without layers.
        provides: Each feature set provided, mapped to its method's name.
    """

    name: str
    archive_layers: frozenset[str]
    buffer_pixels: int
    default_extent: int
    provides: ClassVar[dict[FeatureSet, str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.provides = {
            method.feature_set: attribute_name
            for attribute_name in dir(cls)
            if (method := getattr(cls, attribute_name, None)) is not None
            and hasattr(method, "feature_set")
        }

    def __repr__(self) -> str:
        return f"<TileSchema {self.name}>"

    def extract(self, feature_set: FeatureSet,
                layers: DecodedLayers) -> list[DecodedFeature]:
        """Returns the features of `feature_set` in `layers`.

        Raises:
            KeyError: If the schema doesn't provide `feature_set`.
        """
        return self._method_for(feature_set)(layers)

    def fields_for(self, feature_set: FeatureSet) -> dict[str, str]:
        """Returns property name to MVT type for `feature_set`'s features.

        Raises:
            KeyError: If the schema doesn't provide `feature_set`.
        """
        return self._method_for(feature_set).feature_fields

    def _method_for(self, feature_set: FeatureSet) -> _FeatureMethod:
        """Returns the bound method providing `feature_set`.

        Raises:
            KeyError: If the schema doesn't provide it, naming those it does.
        """
        try:
            name: str = self.provides[feature_set]
        except KeyError:
            available: str = ", ".join(
                sorted(each.name for each in self.provides))
            raise KeyError(
                f"schema '{self.name}' provides no feature set "
                f"{feature_set.name!r} (it provides: {available})") from None
        method: _FeatureMethod = getattr(self, name)
        return method
