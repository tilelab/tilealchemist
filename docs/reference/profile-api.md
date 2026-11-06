# Profile API reference

Everything a profile uses, in the `tilealchemist.profiles` package.

## Contents

- [The profile file](#the-profile-file)
- [Profile](#profile)
- [Tile](#tile)
- [Feature and FeatureSet](#feature-and-featureset)
- [Shipped feature sets](#shipped-feature-sets)
- [Helpers](#helpers)

## The profile file

A profile is a Python file with a `Profile` subclass, named in
`PROFILE: type[Profile] = LandProfile`. tilealchemist constructs it without
arguments, once per process.

A profile can import tilealchemist, `shapely` (2.1 or later), `numpy`,
`pmtiles` and `mapbox-vector-tile`. It declares other packages in one
[PEP 723](https://peps.python.org/pep-0723/) block, which
`tilealchemist setup` installs:

```python
# /// script
# dependencies = ["scipy>=1.11"]
# ///
```

## Profile

`tilealchemist.profiles.base.Profile`. A subclass sets `name` and implements
`transform()`; everything else has a default.

| Member | Default | Description |
| --- | --- | --- |
| `name` | Required | The layer's name and file name. Letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `transform(tile)` | Required | What the output tile contains, a `TransformResult`: a `Feature`, a shapely geometry, an iterable of either, or `None` to leave the tile out. |
| `layer_name` | `name` | The MVT layer's name inside each tile, the `source-layer` of a map style. |
| `layer_fields(schema)` | `{}` | Property names mapped to MVT types: `"String"`, `"Number"` or `"Boolean"`. |
| `vector_layers(schema)` | One entry from `layer_name` and `layer_fields()` | The `vector_layers` of the layer's metadata. |
| `build_tile(tile)` | `transform()`, then encoding | The tile as gzipped MVT bytes, or `None`. |

Geometries use the tile's coordinates, from 0 to `tile.extent`, and may
reach into `tile.buffered_square`. The default `build_tile()` snaps them to
MVT's integer grid, drops those that collapse, and encodes one MVT layer.

## Tile

`tilealchemist.profiles.tile.Tile`, one decoded tile. It stands for every
tile ID with the same content, so it has no zoom level or x/y.

| Member | Description |
| --- | --- |
| `features(feature_set)` | The tile's `Feature`s of a feature set. |
| `buffered_square` | The tile's square grown by the schema's buffer, 64 units for `openmaptiles` and 128 for `protomaps`. |
| `extent` | The coordinate space's size, 4096 in both shipped schemas. |
| `derived(namespace, key, compute)` | `compute(tile)`, computed once and kept on the tile. |
| `layers` | The decoded MVT layers by name. Empty for a gap. Prefer `features()`. |
| `schema` | The run's `TileSchema`. |

## Feature and FeatureSet

Both are in `tilealchemist.profiles.features`, as is `TransformResult`.

- `Feature(geometry, properties={})` is a frozen dataclass.
  `with_geometry(geometry)` returns a copy with the same properties.
- `FeatureSet(name, description)` is a kind of data schemas provide.
  Feature sets compare by identity, so import them, never recreate them. A
  schema without the feature set raises
  `KeyError: "schema 'openmaptiles' provides no feature set 'buildings' (it provides: surface_water, waterways)"`.

## Shipped feature sets

Import them from `tilealchemist.profiles.features`.

| Feature set | `openmaptiles` | `protomaps` |
| --- | --- | --- |
| `SURFACE_WATER`: open water polygons without tunnels, no properties | `water` layer | Polygons of the `water` layer |
| `WATERWAYS`: rivers, streams and canals as lines | `waterway` layer; `class`, `name`, `brunnel`, `intermittent` | Lines of the `water` layer, from zoom level 9; `kind`, `name`, `layer`, `min_zoom`, `sort_rank` |

## Helpers

- `water.surface_water_union(tile)`: the tile's `SURFACE_WATER` as one
  valid geometry, or `None`, computed once per tile. It is grown by half a
  unit so narrow gaps can't snap into slivers of land.
- `water.subtract_water(tile, geometry)`: `geometry` minus that union.
  Profiles that cut against it meet without seams.
- `mvt.encode_tile(layers, extent)`: encodes `{MVT layer name:
  TransformResult}` as one gzipped MVT tile, snapped like the default
  `build_tile()`. Empty layers are dropped; `None` if all are.
