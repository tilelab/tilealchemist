# Customize a profile's output

Each section changes one thing about what a profile writes. All members are
described in the [profile API reference](../reference/profile-api.md).

## Keep the properties of source features

Return the source features with `with_geometry()`, and declare their
properties in `layer_fields()`:

```python
def layer_fields(self, schema: TileSchema) -> dict[str, str]:
    return schema.fields_for(WATERWAYS)

def transform(self, tile: Tile) -> list[Feature]:
    return [waterway.with_geometry(
                water.subtract_water(tile, waterway.geometry))
            for waterway in tile.features(WATERWAYS)]
```

For properties of your own, return `Feature(geometry, {"area": ...})` and
declare `{"area": "Number"}`.

## Rename the MVT layer

A map style refers to the MVT layer inside each tile by name, its
`source-layer`. It defaults to the profile's `name`:

```python
@property
def layer_name(self) -> str:
    return "landcover"
```

## Leave tiles out

Return `None` or `[]` from `transform()`. Geometries that collapse on MVT's
integer grid are dropped too, and a tile left with none is left out.

## Write several MVT layers into one tile

Override `build_tile()` to hand several MVT layers to `mvt.encode_tile()`,
and `vector_layers()` to describe them in the metadata. Each takes what
`transform()` may return. This writes land and water as two MVT layers:

```python
def vector_layers(self, schema: TileSchema) -> list[dict[str, Any]]:
    return [{"id": "land", "fields": {}}, {"id": "water", "fields": {}}]

def build_tile(self, tile: Tile) -> bytes | None:
    return mvt.encode_tile({"land": self.transform(tile),
                            "water": water.surface_water_union(tile)},
                           tile.extent)
```

## Build tiles for gaps

Only tilealchemist-pipeline builds gaps, tile IDs the archive has no tile
for. It hands each to the profile as a tile without layers. Most profiles
need nothing special: an empty tile has no features, and the land profile
turns it into a full square. To build something else, check
`not tile.layers`.

## Share a computed value between profiles

`tile.derived(namespace, key, compute)` computes `compute(tile)` once and
keeps it on the tile, which every profile of the run shares:

```python
def _named_waterways(tile: Tile) -> list[Feature]:
    return [waterway for waterway in tile.features(WATERWAYS)
            if waterway.properties.get("name")]

def transform(self, tile: Tile) -> list[Feature]:
    return tile.derived("my_helpers", "named_waterways", _named_waterways)
```

Use a namespace of your own; the water helpers use `"water"`.
