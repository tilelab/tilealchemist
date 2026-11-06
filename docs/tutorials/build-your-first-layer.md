# Build your first layer

You build two layers for Berlin from a Protomaps archive: land without its
water, and waterways cut to that land. It takes about 15 minutes. You need
Python 3.11 or later and the `pmtiles` command from
[go-pmtiles](https://github.com/protomaps/go-pmtiles/releases).

## Step 1: Install tilealchemist

```sh
mkdir first-layer && cd first-layer
python3 -m venv .venv && source .venv/bin/activate
pip install tilealchemist
```

## Step 2: Get a small archive

Copy Berlin up to zoom level 12, about 7 MB, out of the newest Protomaps
daily build:

```sh
BUILD=$(curl -s https://build-metadata.protomaps.dev/builds.json | python3 -c 'import json, sys; print(max(build["key"] for build in json.load(sys.stdin)))')
pmtiles extract "https://build.protomaps.com/$BUILD" berlin.pmtiles --bbox=13.08,52.33,13.76,52.68 --maxzoom=12
```

## Step 3: Write a profile

A **profile** decides what each tile of a layer contains:

```python
# land.py
from shapely.geometry.base import BaseGeometry

from tilealchemist.profiles import water
from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.tile import Tile


class LandProfile(Profile):
    name: str = "land"

    def transform(self, tile: Tile) -> BaseGeometry:
        return water.subtract_water(tile, tile.buffered_square)


PROFILE: type[Profile] = LandProfile
```

`transform()` runs once per tile: it takes the tile's square and cuts out
the tile's water. `name` names the layer, and `PROFILE` marks the class.

## Step 4: Write the run config

The **run config** lists the profiles and names the archive's schema:

```toml
# layers.toml
schema = "protomaps"
profiles = ["land.py"]
```

## Step 5: Build the layer and look at it

```sh
tilealchemist convert berlin.pmtiles layers.toml
pmtiles show land.pmtiles
```

`convert` ends with `layer: ./land.pmtiles`. `pmtiles show` prints the
layer's zoom range, its bounds and the OpenStreetMap attribution copied from
the archive. To see the tiles, drop the file onto
[pmtiles.io](https://pmtiles.io).

## Step 6: Add a second layer

```python
# waterways.py
from tilealchemist.profiles import water
from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.features import WATERWAYS, Feature
from tilealchemist.profiles.tile import Tile
from tilealchemist.schemas.base import TileSchema


class WaterwaysProfile(Profile):
    name: str = "waterways"

    def layer_fields(self, schema: TileSchema) -> dict[str, str]:
        return schema.fields_for(WATERWAYS)

    def transform(self, tile: Tile) -> list[Feature]:
        return [waterway.with_geometry(
                    water.subtract_water(tile, waterway.geometry))
                for waterway in tile.features(WATERWAYS)]


PROFILE: type[Profile] = WaterwaysProfile
```

- `tile.features(WATERWAYS)` asks for a **feature set**, a kind of data,
  rather than an archive layer, so the profile works with every schema that
  provides it.
- `with_geometry()` keeps each waterway's properties, and `layer_fields()`
  declares them in the layer's metadata.

Add the profile to `layers.toml` and build again. `--overwrite` replaces the
existing `land.pmtiles`:

```toml
profiles = ["land.py", "waterways.py"]
```

```sh
tilealchemist convert berlin.pmtiles layers.toml --overwrite
```

Both layers come from one pass: each tile is decoded once for both profiles.

## Next steps

- [Core concepts](../explanation/concepts.md) explains how the pieces fit
  together.
- [Customize a profile's output](../how-to-guides/customize-profile-output.md)
  shows what else a profile can do.
- [tilealchemist-pipeline](https://github.com/tilelab/tilealchemist-pipeline)
  builds layers for the whole planet.
