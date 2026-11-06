# Schemas and plugins reference

A **schema** knows how an archive's tiles encode their data and answers
feature sets from them. A **plugin** is an installed package that adds
schemas, commands or sources.

## Shipped schemas

| | `openmaptiles` | `protomaps` |
| --- | --- | --- |
| Reads | OpenMapTiles archives, such as OpenFreeMap's | Protomaps basemap archives |
| `archive_layers` | `water`, `waterway` | `water`, `earth` |
| Buffer, extent | 4 pixels, 4096 | 8 pixels, 4096 |

## TileSchema

`tilealchemist.schemas.base.TileSchema`. A subclass sets the attributes
below and marks one method per feature set with `@feature`:

```python
@feature(WATERWAYS, fields={"class": "String", "name": "String"})
def waterways(self, layers: DecodedLayers) -> list[DecodedFeature]:
    return layer_features(layers, "waterway")
```

The method gets the tile's decoded layers and returns decoded features,
dicts with `"geometry"` and `"properties"`. `layer_features(layers, name)`
returns one layer's features, or `[]`. `fields` declares the properties for
`fields_for()`.

| Member | Description |
| --- | --- |
| `name` | The name run configs use. Must equal the entry point's name. |
| `archive_layers` | Layers every archive in the schema lists in its metadata, enough to tell it from other schemas. |
| `buffer_pixels` | How far features reach past the tile edge, in pixels of a 256-pixel tile. |
| `default_extent` | The extent of a tile without layers. |
| `extract(feature_set, layers)` | The features of a feature set. |
| `fields_for(feature_set)` | The feature set's property names and MVT types. |

## The schema check

Before the first tile, `convert` checks that the archive's metadata lists
every layer in `archive_layers`, and otherwise names the schemas that fit:

```
tilealchemist convert: planet.pmtiles has no waterway layer, so it isn't in schema openmaptiles; set the config's `schema` to 'protomaps'
```

An archive without `vector_layers` in its metadata is read unchecked, with a
warning.

## Plugins

Plugins declare [entry points](https://packaging.python.org/en/latest/specifications/entry-points/)
in these groups:

| Group | Each entry names |
| --- | --- |
| `tilealchemist.schemas` | A `TileSchema` instance. |
| `tilealchemist.commands` | A function `add_parser(subparsers)` adding a `tilealchemist` command. |
| `tilealchemist.sources` | A tilealchemist-pipeline [source](https://github.com/tilelab/tilealchemist-pipeline/blob/main/docs/reference/sources.md). |

tilealchemist declares its own schemas the same way. A schema's
entry-point name must equal its `name`, and two packages must not declare
the same name. Install plugins with the run config's `plugins` and
`tilealchemist setup`, or with pip, in every environment that runs the run.
