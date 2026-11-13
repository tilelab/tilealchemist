# Write a schema plugin

A schema plugin lets profiles read data no shipped feature set provides, or
archives in another schema. This guide builds `tilealchemist-borders`, which
reads country borders from OpenMapTiles archives. A feature set both shipped
schemas could provide is welcome in tilealchemist itself; see
[CONTRIBUTING.md](../../CONTRIBUTING.md#feature-sets).

## Step 1: Write the schema

Subclass the schema you extend, add the archive layers it reads, and add one
`@feature` method per new feature set. The subclass keeps the parent's
feature sets, so existing profiles still work with it.

```python
# tilealchemist_borders/__init__.py
from tilealchemist.profiles.features import FeatureSet
from tilealchemist.profiles.mvt import DecodedLayers
from tilealchemist.schemas.base import DecodedFeature, feature, layer_features
from tilealchemist.schemas.openmaptiles import OpenMapTilesSchema

BORDERS: FeatureSet = FeatureSet("borders", "Country borders on land.")


class OpenMapTilesBordersSchema(OpenMapTilesSchema):
    # Must match the entry point's name.
    name: str = "openmaptiles-borders"
    archive_layers: frozenset[str] = (OpenMapTilesSchema.archive_layers
                                      | {"boundary"})

    @feature(BORDERS, fields={"admin_level": "Number",
                              "disputed": "Number", "maritime": "Number"})
    def borders(self, layers: DecodedLayers) -> list[DecodedFeature]:
        return [line for line in layer_features(layers, "boundary")
                if line["properties"].get("admin_level") == 2
                and not line["properties"].get("maritime")]


OPENMAPTILES_BORDERS: OpenMapTilesBordersSchema = OpenMapTilesBordersSchema()
```

## Step 2: Declare the entry point

```toml
# pyproject.toml
[project]
name = "tilealchemist-borders"
version = "1.0.0"
dependencies = ["tilealchemist"]

[project.entry-points."tilealchemist.schemas"]
openmaptiles-borders = "tilealchemist_borders:OPENMAPTILES_BORDERS"
```

## Step 3: Use it

A profile imports the new feature set like any other:

```python
# borders.py
from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.features import TransformResult
from tilealchemist.profiles.tile import Tile
from tilealchemist_borders import BORDERS


class BordersProfile(Profile):
    name: str = "borders"

    def transform(self, tile: Tile) -> TransformResult:
        return tile.features(BORDERS)


PROFILE: type[Profile] = BordersProfile
```

The run config names the schema and the plugin, and `setup` installs it:

```toml
# layers.toml
schema = "openmaptiles-borders"
plugins = ["./tilealchemist-borders"]
profiles = ["borders.py"]
```

```sh
tilealchemist setup layers.toml
tilealchemist convert planet.pmtiles layers.toml
```

Run `setup` wherever `convert` runs. If something goes wrong, the error
says what to fix; the rules are in
[Schemas and plugins](../reference/schemas.md#plugins).
