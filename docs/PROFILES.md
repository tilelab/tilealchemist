# Profiles

The `Profile` system: the contract every profile implements, the `Tile` it
is handed, `TileSchema` and its feature sets, the shared helpers a profile
can (but doesn't have to) use, and how to write and distribute a profile of
your own. This package ships no profiles: for two real ones, and what they
compute, see
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)
and its `land` and `cropped-waterways`, referred to throughout this document
as worked examples. For the pipeline mechanics around a profile (fetching,
sharding, publishing), see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

## The `Profile` contract

A profile owns the per-tile transform and the output layer's naming and
metadata, and nothing else: it never touches fetching, sharding, or how tiles
reach it (`shard_worker.py`), the gzip+protobuf MVT codec, or snapping its
output onto the encoder's integer grid (`mvt.py` does that for everything it
encodes; see [the output grid](#the-output-grid)). Within that, it
implements two things, and everything else in
`tilealchemist/profiles/base.py` has a working default:

```python
class Profile(ABC):
    name: str        # identifier for logging and, by default, the output layer
                     # and the layer's metadata name

    def transform(self, tile): ...   # the actual per-tile work
```

A profile is **constructed with no arguments and holds no per-run state**. In
particular it holds no schema: `transform()` gets everything schema-specific
from the `Tile` it is handed (which carries both the extent and the schema),
and the two metadata-time methods that genuinely need a schema,
`output_fields()` and `transform_gap()`, take one as an argument. That
keeps a profile schema-independent by construction rather than by
convention, and stops `shard_worker.py` having to reach back through a
profile for a schema it already has.

That really is the whole mandatory surface, so here is `land` in full:

```python
class LandProfile(Profile):
    name = "land"

    def transform(self, tile):
        return water.subtract_water(tile, tile.buffered_square)

PROFILE = LandProfile
```

- **`transform(tile)`** gets a `Tile` (see below) and returns this profile's
  output for it. The return value is normalized: you can return a single
  `Feature`, a single shapely geometry (wrapped automatically as one
  property-less `Feature`), an iterable of either mixed freely, or `None`/`[]`
  to skip the tile entirely (write nothing for it, same as a missing tile in
  any vector tileset). No extent to return, no snapping to remember, no
  decoding or encoding to do, and no filtering out of features an overlay
  left empty, since those are dropped at encoding time (see `mvt.py` below).

  Geometry is shapely throughout, and features are `Feature` objects
  throughout: `tile.features()` hands you `Feature`s, `transform()` gives
  `Feature`s back. Both are deliberate narrowings. shapely is already a hard
  dependency (`mvt.py` and `water.py` both use it), and passing raw
  MVT-decoded dicts around meant every shared helper had to guess what it had
  been given; `{"geometry": ..., "properties": ...}` dicts belong to
  `mapbox_vector_tile` and now stay at its decoder and encoder, instead of
  being the currency profile code is written in.

The defaults a profile can override, but usually doesn't:

- **`output_layer_name`** / **`mbtiles_name`** both default to `name`.
- **`output_fields(schema)`** declares the MVT field types of the properties
  this profile's *output* features carry, for `vector_layers_json()`. Empty by
  default, which is right for a profile emitting no properties at all
  (`land`). A profile passing source properties through returns the schema's
  declaration for them, so `cropped-waterways` is one line:
  `return schema.fields_for(WATERWAYS)`. Deliberately the profile's
  own call rather than derived from the feature sets it reads: which input
  fields survive into the output is exactly what the transform decides.
- **`vector_layers_json(schema)`** returns the `vector_layers` array embedded in
  the output layer's metadata, as is: nothing downstream adds the fields it
  sees in the tiles, so what `output_fields()` declares is what a consumer
  reads. The default,
  `[{"id": output_layer_name, "fields": output_fields()}]`, covers every
  profile that writes a single output layer, which is what `transform()`'s
  contract describes.
- **`transform_tile(tile)`** hands the tile to `transform()` and encodes
  whatever comes back. Overridable directly for a profile with unusual needs
  (non-MVT output, full control over encoding); it just needs some
  `transform()` (even a stub) to satisfy the ABC.
- **`transform_gap(schema)`** returns the bytes written at every *gap* tile: a
  `tile_id` entirely absent from the source archive (see
  [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#fetching-directory-driven-not-one-request-per-tile)
  "Fetching" step 5). `None` means write nothing for gap tiles at all. A
  once-per-run call, not a per-tile one: a gap tile is just a tile with no
  layers, so the default runs `transform()` over a `Tile.empty(schema)` and
  there's nothing left for a `(z, x, y)` to vary. The worker calls it
  once per profile and reuses the result across the whole gap, which can be
  hundreds of thousands of tiles (e.g. an ice sheet interior).
- **`seconds_per_tile`** is what `prepare-shards` sizes the run's *time*
  with: what `transform_tile()` costs on one deduped source tile. It is a
  profile's own estimate — the work is its shapely, not tilealchemist's —
  and the default is deliberately pessimistic, so an undeclared profile
  plans a bigger run rather than one that runs out of time. See
  [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#why-no-budget-caps-a-worker)
  "Why no budget caps a worker" for the caps that were tried and dropped.
- **`bytes_per_output_tile`** is what one of this profile's output tiles
  weighs in the part. The write is charged on bytes rather than on tiles,
  because a tile is only as expensive to store as it is large, and how large
  it is belongs to the profile that shaped it: a coastline profile's tiles are
  not a label profile's. It still does not *bound* anything — nothing caps what
  a worker writes, and a run is sized on time alone.

- **`gap_bytes(schema)`** is a method, not a figure, because the answer is
  knowable exactly rather than worth estimating: the default implementation
  measures your own `transform_gap()` once, at plan time. Override it only if
  your profile can answer without building the tile.

  Neither declared figure is a promise you have to get right. A pipeline keeping
  its measurements on a state branch replaces both, per tile block, with what
  the last few runs actually cost this archive and this set of profiles, and a
  declared value only prices the blocks nothing has measured yet; see
  [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#the-state-branch-and-the-job-that-writes-it)
  "The state branch, and the job that writes it".
- **`_encode_tile(features, extent)`** wraps `mvt.encode_tile()` with this
  profile's own `output_layer_name` filled in, and is the one place a profile
  touches the MVT codec directly, for a `transform_gap` or
  `transform_tile()` override that produces encoded bytes itself. It is also
  where `Feature` ends and `mapbox_vector_tile`'s own
  `{"geometry": ..., "properties": ...}` dict begins: `mvt.py` deals in the
  library's format in both directions, so this pipeline's vocabulary stops at
  the profile layer.

Beyond the class, every profile's `.py` file needs a module-level
`PROFILE = YourProfileClass` assignment (the class, not an instance):
`load_profile()` in `tilealchemist/profiles/__init__.py` looks this up by
that exact name after importing the file, `--profile`'s only way of finding
which class to instantiate.

The generic worker (`shard_worker.py` and `transform.py`) only ever uses `name`,
`vector_layers_json()`, `transform_tile()`, `mbtiles_name`, and
`transform_gap`, nothing profile-specific.

## `Tile`

`tilealchemist/tile.py`'s `Tile` is what `transform()` is handed: one
decoded source tile, the schema that explains it, and a place for anything
derived from it to be computed once and shared.

```python
class Tile:
    layers                       # decoded {layer_name: {...}} dict
    schema                       # the TileSchema this tile is read through
    extent                       # this tile's MVT coordinate extent
    buffered_square              # tile square, buffered past the edge (shapely)
    def features(self, feature_set)   # this tile's Features for a FeatureSet
    def derived(self, namespace, key, compute)   # memoize your own intermediate
```

It exists to keep three things out of the `Profile` contract:

- **Extent.** An MVT layer carries its own, so without this every profile had
  to fish one out of whichever layer it happened to read and invent a
  fallback for tiles that had none. `Tile.extent` is that one value (taken
  from the first layer in the source tile's own layer order; the schema's
  `default_extent` for a tile with no layers at all), and `transform()` no
  longer returns it.
- **Sharing.** Two profiles running back-to-back over the same tile (`land` +
  `cropped-waterways`) must not decode it twice or union its water twice.
  `transform.py` builds one `Tile` per source tile and hands that same
  object to every profile, so the lifetime of a derived value is the tile's.
  `features()` and `derived()` are the whole mechanism; there is no global
  cache anywhere, and nothing depends on the transform loop keeping a
  particular iteration order.
- **Gap tiles.** A `tile_id` absent from the source archive is just a tile
  with no layers, which `Tile.empty(schema)` says directly.
- **Geometry.** `features()` turns each raw decoded feature into a `Feature`
  with shapely geometry once per tile, so a profile never calls `shape()`
  itself and every shared helper knows what it is being handed.
  `buffered_square` is here for the same reason: it is a property of the
  tile under its schema (how much geometry past the edge this tile format
  carries), not of any one layer, so every profile reasoning about the tile
  edge agrees on where that edge is.

## `Feature` and `FeatureSet`

`tilealchemist/features.py` holds the vocabulary a profile works in.

A **`Feature`** is what a profile reads and writes: a shapely geometry plus the
properties it carries.

```python
@dataclass(frozen=True)
class Feature:
    geometry: BaseGeometry
    properties: dict = field(default_factory=dict)

    def with_geometry(self, geometry): ...   # same properties, new geometry
```

`with_geometry()` is the common shape of a transform (cut, clip or simplify
the geometry, carry the source properties through untouched), and says that in
one call instead of rebuilding a dict by hand and risking a dropped key. The
whole of `cropped-waterways`:

```python
def transform(self, tile):
    return [waterway.with_geometry(
                water.subtract_water(tile, waterway.geometry))
            for waterway in tile.features(WATERWAYS)]
```

A **`FeatureSet`** is one of the named kinds of source data a schema can
provide and a profile can ask for.

```python
SURFACE_WATER = FeatureSet("surface_water",
    "Real, non-tunnel surface water polygons: what a renderer would paint "
    "as open water. Water running through a tunnel is not part of this.")
```

A profile imports the feature sets it reads and asks for them by object, not
by string:

```python
from tilealchemist.features import WATERWAYS

for source_feature in tile.features(WATERWAYS):
    ...
```

A feature set belongs to no schema, and that's the point. `WATERWAYS` states a
meaning, and every schema able to answer it declares a `@feature(WATERWAYS)`
method, however differently its own layers are structured. So a profile names
what it needs without ever naming a schema.

Objects rather than strings buy three things: a typo is an `ImportError` at
startup instead of a `KeyError` a few million tiles into a shard; editors can
complete and jump to the definition; and there is finally one place where a
feature set's *meaning* is written down, instead of it being restated in each
schema's docstring and drifting apart.

Adding a feature set for a domain no schema covers yet (buildings, landuse,
POIs) is an entry in `features.py` plus a `@feature` method on the schemas
that can answer it. Neither `TileSchema` nor `Profile` changes, so no existing
schema or profile breaks.

## `TileSchema`

`tilealchemist/schemas.py`'s `TileSchema` is an ABC, the same pattern as
`Source` and `Profile` (see `sources/base.py`/`profiles/base.py`): a schema
implements *behavior* a profile calls, not config a profile reads and
interprets itself.

A schema **answers feature sets**: each method marked `@feature(SOME_SET)`
pulls that one meaning out of a decoded tile's layers. Which method name it
uses is the schema's own business; the `FeatureSet` it declares is what a
profile asks for.

```python
class OpenMapTilesSchema(TileSchema):
    name = "openmaptiles"
    default_buffer_pixels = 4

    @feature(SURFACE_WATER)
    def surface_water(self, layers):
        water = layers.get("water")
        if not water:
            return []
        return [f for f in water["features"]
                if f["properties"].get("brunnel") != "tunnel"]

    @feature(WATERWAYS, fields={"class": "String", "name": "String",
                                "brunnel": "String", "intermittent": "Boolean"})
    def waterways(self, layers):
        waterway = layers.get("waterway")
        if not waterway:
            return []
        return waterway["features"]
```

A `@feature` method returns the raw per-feature dicts
`mapbox_vector_tile.decode()` produces, or an empty list if this schema has no
such data in this tile. It hands geometry straight through unparsed, because
`TileSchema`'s job is schema-structure knowledge only: which layer, which
attribute means "exclude this".

Profiles never see those raw dicts. `Tile.features()` turns each one into a
`Feature` (shapely geometry, properties) once per tile before handing the
list on, so that conversion lives in one place instead of in every profile and
every shared helper.

`fields=` declares the MVT field types of the properties a feature set
carries, reachable as `schema.fields_for(WATERWAYS)` for a profile that
passes them through to its own output layer. It lives on the schema because
field names and types belong to the schema and aren't guaranteed to match
across schemas.

**Why behavior instead of a plain layer-name lookup table:** schemas can
differ *structurally*, not just by naming, in ways a name-to-name dict can't
express. The two shipped here are already an example. OpenMapTiles keeps
water polygons in a `water` layer and waterway lines in a separate
`waterway` one, both classified by a single string `brunnel` attribute;
Protomaps has no waterway layer at all, putting polygons, lines and label
points in one `water` layer told apart by *geometry type*, and sets
`tunnel` (raw OSM tag, z14 and up) on polygons only. No pair of layer names
maps one onto the other, but both answer `SURFACE_WATER` and `WATERWAYS`,
so the `land`/`cropped-waterways` profiles read either unmodified: neither
profile names a layer or an attribute anywhere.

Schemas this pipeline doesn't ship differ again in the same way. Shortbread
(checked against its real [1.0 schema
docs](https://shortbread-tiles.org/schema/1.0/), not assumed) splits water
across an `ocean` and a `water_polygons` layer and uses two boolean fields,
with no tunnel concept on polygons at all; a subclass for it writes its own
`surface_water()` and nothing else in the pipeline moves.

**Why feature sets aren't fixed abstract methods on the ABC:** so that a
profile for a domain no schema covers yet (buildings, landuse, POIs) is
written by declaring the `FeatureSet` and adding a `@feature` method to the
schemas that can answer it, never by adding an `@abstractmethod` here and
breaking every existing schema. The extension point is the schema, not the
base class.

`default_buffer_pixels`/`tile_size_pixels` stay plain fields, not feature
sets: they describe the *tile format itself* (how much edge-buffer geometry
its tiles carry, and what pixel size that's relative to), the same for any
profile reasoning about tile edges, regardless of what layer it's looking at.

Two schemas exist today, `openmaptiles` and `protomaps`. A third is its own
`TileSchema` subclass plus one `SchemaName` member and one entry in
`tilealchemist/schemas.py`'s `SCHEMAS` dict, and nothing else: no profile,
and no other module, changes. A schema's `name` is that `SchemaName` member
rather than a bare string, so the one name that travels between processes
and files (`source.json`, `--schema`, a transform pool worker) is a closed
set every reader can check against.

Which one a run reads is not usually a choice at all: a `Source` names the
schema its provider publishes, and `prepare_shards.py` writes that name into
`source.json` alongside the URL it resolved, so every worker reads the
archive through the schema the archive is actually in. See
docs/ARCHITECTURE.md "Source resolution" for the one source that has to be
told (`static-url`) and what `--schema` does there.

## No compatibility checks needed

A schema declares which feature sets it answers (via `@feature(SOME_SET)`
methods), and a profile asks for the ones it needs. There is no
hand-maintained per-profile list of compatible schemas and no early validation
pass: compatibility is whatever both sides actually declare, so it can't
quietly go stale.

Most mistakes never reach that check: a profile imports the feature sets it
reads, so a misspelled one fails at startup, before any fetching:

```
ImportError: cannot import name 'WATERWYAS' from 'tilealchemist.features'
```

Pairing a profile with a schema that genuinely can't answer it is a real
mismatch rather than a typo, and raises where the profile asks:

```
KeyError: "schema 'openmaptiles' provides no feature set 'buildings' (it provides: surface_water, waterways)"
```

## Shared helpers a profile can use

- **`tilealchemist/mvt.py`** (`decode_tile()`/`encode_tile()`) wraps the
  gzip+MVT codec every profile ends up needing, since both the source
  archive's tiles and this pipeline's own output rows are gzipped MVT. Not
  something a profile calls directly in the common case: `Tile.decode()` and
  `Profile.transform_tile()`/`_encode_tile()` handle it generically, since
  it's always MVT regardless of which profile is running. `encode_tile()`
  also snaps every geometry it is given onto `OUTPUT_GRID_SIZE` and drops
  anything that collapses to nothing, so a profile never has to; see
  [the output grid](#the-output-grid) below for what that is protecting it
  from.
- **`tilealchemist/water.py`** is geometry math for profiles that need to
  subtract a tile's real surface water from something:
  `subtract_water(tile, geometry)` and `surface_water_union(tile)`, both
  taking and returning shapely geometry (not `Feature`s; they are geometry
  math, so a profile passes `some_feature.geometry` in). Scoped to
  water-related profiles specifically; a profile working on buildings, POIs,
  or anything else has no reason to touch it, and it is not part of the
  `Profile` contract. See [subtracting water](#subtracting-water) below for
  why it is a function a profile calls rather than a union it fetches and
  cuts with itself.

### The output grid

An MVT tile's coordinates are integers relative to its extent, by spec. That
integer grid is `mvt.OUTPUT_GRID_SIZE` (one unit), and `encode_tile()` snaps
every geometry onto it, topology-aware via `shapely.set_precision(...,
mode="valid_output")`, before handing it to the encoder.

**A profile does not have to do this itself, and gains nothing by doing it.**
It is documented because the cost of the snap *not* happening is not a
slightly worse tile, which is the intuition the rest of this pipeline trains.

The encoder rounds coordinates to integers either way. The only question is
whether that rounding is topology-aware, and the unaware version breaks
valid input in two ways:

- **Parts that only collide once each is rounded on its own.** Two polygons
  0.4 units apart, the routine result of cutting one shape against another,
  round into a self-intersecting `MultiPolygon`. The library validates each
  part in isolation, so nothing there repairs a conflict *between* parts, and
  `encode_tile()` passes `on_invalid_geometry_raise`, so this aborts the
  whole shard.
- **Anything narrower than one unit.** It rounds to a zero-area ring, which
  aborts as above, or to a zero-length line, which the encoder drops so
  quietly that a tile with nothing left in it is still written, as a row
  holding an empty layer, where the profile meant to write no row at all.

Snapping first removes both: the encoder receives valid, already-on-grid
geometry its own rounding cannot alter, and a feature that collapsed is
visibly empty beforehand, so `encode_tile()` skips it and returns `None` for
a tile with nothing left. It is also what makes `on_invalid_geometry_raise`
the right setting rather than a hair trigger: it can now only fire on
geometry that was already broken upstream, which is worth hearing about
loudly.

Which profiles this actually matters for: every one of them, this
repository's own included. A fixed-precision overlay does not exempt a
profile, tempting as that assumption is. Measured on tile 9/269/151, a
`grid_size=` cut still moved under `snap_to_output_grid()`, from area
12357033.50 to 12357060.00, because the snap drops what collapses below a
unit as well as rounding. For `land` and `cropped-waterways`, which pass no
`grid_size=` at all (see "Subtracting water"), this snap is the only thing
putting their output on the grid. It is equally load-bearing for the
profile that
builds geometry any other way: `buffer()`, `simplify()`, a hand-built shape,
or a default float-precision overlay. Those live outside this repository, so
that is the normal case, not the exotic one, which is also why the snap sits
in `encode_tile()` rather than in each profile. A profile that forgot it
would get no warning: the failure surfaces as an aborted shard or a phantom
tile, neither of which points back at the omission.

### Subtracting water

`water.py` answers one question for a profile, *what is left of this geometry
once the tile's real surface water is taken out of it*, and answers it the
same way for every caller. `surface_water_union(tile)` is the union of the
tile's `SURFACE_WATER` polygons (`None` if it has none), computed once per
tile through `Tile.derived()` however many profiles ask;
`subtract_water(tile, geometry)` cuts that union out of a geometry the profile
supplies. Neither knows anything about layers or attributes: deciding *which*
polygons count as real surface water is the schema's job (see the
`SURFACE_WATER` feature set), and this is only the geometry step afterwards.

**Why an operation rather than an operand.** A profile could fetch the union
and call `difference()` itself, and the two-line version of each water profile
would look no worse for it. Two decisions inside `subtract_water()` are the
reason it doesn't. Neither is visible from a call site, and getting either
wrong doesn't produce a slightly worse tile: one shows up as a rendering
artifact across every coastline in the build, the other aborts the shard. They
also have to come out the same for every profile in a run: `land` inverts the
water while `cropped-waterways` cuts lines against it, so if the two computed
even slightly different water, a cropped river would no longer meet the land
polygon it should butt up against, and the seam between them would be visible.

**The union is buffered out by half a grid unit** (`WATER_GAP_CLOSING_BUFFER`)
before anything is cut with it. Adjoining water features that don't share
vertices (a river polygon meeting a lake, or meeting the coastline) can
leave a hairline gap between them, a fraction of a unit wide. Left alone that
gap counts as land, and snapping onto [the output grid](#the-output-grid)
rounds it up to a whole unit: a sub-pixel numerical crack becomes a visible
sliver of land drawn across the water (seen at a river mouth in tile
14/8637/5296). Buffering the union out by half a cell absorbs any gap narrower
than one output unit before it can be rounded into one. The cost is shrinking
every coastline by that same half unit, 1/32 px at any zoom, imperceptible in
a viewer like MapLibre GL.

**The cut runs at default float precision, and the operand is valid by
construction.** What carries the cut is the validity of the union, not the
precision model of the overlay, which is a correction of an earlier design
here worth stating plainly because the reasoning looked sound: real OSM water
does produce near-coincident geometry, and the failure is a
`TopologyException` that takes a whole shard down rather than one tile, so
forcing GEOS's fixed-precision overlay with `grid_size=OUTPUT_GRID_SIZE`
seemed the obvious defence.

The case that settled it: openfreemap planet build `20260906_080001`,
`openmaptiles` schema, profile `land`, the `subtract_water(tile,
tile.buffered_square)` in its `transform()`, tile 9/269/151 (southern
Norway), shapely 2.1.2, raising `TopologyException: unable to assign free
hole to a shell` at 2292.538, 471.809. The cause sat upstream of the cut:
`WATER_GAP_CLOSING_BUFFER` pinched a narrow skerry off the union's shell and
GEOS emitted the remnant as a separate 2.209-unit element overlapping the sea
it came from, leaving the union invalid (`Nested shells` at 2292.535,
471.817). The *unbuffered* union cut fine under float. Fixed precision had
been hiding that defect for this one consumer while every other operation on
the same union, `symmetric_difference()` among them, still broke on it. So
the repair belongs in `_compute_union()`, and that is where it is.

With a valid operand, fixed precision bought nothing measurable and cost
real time: ~2.7x on that tile (34.4ms against 12.5ms), and its output was
*not* geometry the encoder's snap leaves alone, which had been the other
argument for it (see "The output grid"). Uniformity across profiles, the
remaining argument, comes from every profile calling this one function. So
`grid_size=` is gone.

That removal rests on one tile, which is worth being honest about: it shows
this failure was a validity defect rather than a float-overlay limit, not
that the float overlay never chokes on water geometry elsewhere. If a build
throws a `TopologyException` out of this cut again with a valid union, put
`grid_size=` back and record the tile here.

**Every caller gets that same cut against that same union**, whatever the
dimension of what it is cutting. An earlier version snapped the union onto the
output grid first for *line* geometry only, on the grounds that a line against
a polygon can throw `side location conflict` even under fixed precision. That
was measured against real OpenFreeMap tiles and removed:

- It did not prevent anything reproducible. Across ~37,600 waterway lines on
  z10/z12/z14 tiles (40 anchor regions worldwide plus dense coverage of the
  Ganges, Mekong, Mississippi, Volga, Po and Danube deltas, the Netherlands,
  the Finnish lakes and the Norwegian fjords), no cut threw, with or without
  the pre-snap. Nor did the same lines cut against a deliberately unrepaired
  union (no `buffer(0)`, no gap buffer) or under the plain float overlay, on
  either GEOS 3.11.4 or GEOS 3.13.1.
- It was not free. 11 to 16% of cut lines came out differently with it,
  *after* the encode-time snap, and 347 of ~15,000 differed by more than one
  grid unit, in the worst case a whole segment kept by one variant and
  dropped by the other.
- It worked against the point of this module. Lines were cut against the
  snapped union while the land polygon was cut against the unsnapped one, so
  the two profiles disagreed by up to half a unit about where the water ended.
  Measured over 6,256 lines, that left 218 units of cropped line lying on top
  of rendered water (60 lines) versus 50 units (17 lines) without it, four
  times more of exactly the doubled-up stroke the cropping exists to remove.

If a `side location conflict` ever does surface here, the fix is a fallback on
the exception, not a pre-snap on every line: the snapped union is a different
answer, not a safer route to the same one.

**`make_valid()` repairs the union, it does not reshape it.** GEOS's buffer
can pinch a narrow hole (a skerry) off the union's shell and emit the remnant
as a duplicate element overlapping the body it came from, leaving the union
invalid with `Nested shells`. `_compute_union()` repairs that there rather
than at the cut, because an invalid union is undefined behaviour for every
consumer. `method="structure"` is required: the default `"linework"` splits
the overlap instead of merging it, leaving a phantom island where the
duplicate was — the artefact class `WATER_GAP_CLOSING_BUFFER` exists to
prevent. `"structure"` merges it, measurably identical to unioning the parts
with each other, and `keep_collapsed=False` keeps the result polygonal so no
caller is handed a line where it expects an area. This is why `pyproject.toml`
pins `shapely>=2.1`, the first release with `make_valid(method=...)`.

### What the schemas declare, and where those values came from

The two schemas this repository ships differ in more than layer names, and
the constants in `schemas.py` are measurements rather than preferences. They
are recorded here because nothing in the code can show where they came from.

`OpenMapTilesSchema` has one `water` polygon layer, one `waterway` line
layer, and tunnel/bridge/ford classification in one string attribute,
`brunnel`. Its `default_buffer_pixels = 4` is Planetiler's own default
(`defaultBufferPixels` in `FeatureCollector`), which OpenMapTiles leaves
unchanged for `water`/`waterway` (`BUFFER_SIZE` in
`OpenMapTilesSchema.java`). Label layers override it much wider — `place`
uses 256 — which is why the name says *default*. Its `WATERWAYS` fields
follow the [OpenMapTiles schema docs](https://openmaptiles.org/schema/#waterway).

`ProtomapsSchema` (basemap v4) has no waterway layer at all. Water polygons,
waterway lines and water label points share one `water` layer, told apart by
the geometry type `mapbox_vector_tile.decode()` writes into each feature's
geometry dict — the same way Protomaps' own styles read it, their water fill
layer filtering `["==", "$type", "Polygon"]`. Its `default_buffer_pixels = 8`
is the wider of the schema's two: `Water.java`/`Earth.java` call
`setBufferPixels(8)` on water polygons and on `earth`, while water *lines*
keep Planetiler's default 4, and a profile reasoning about tile edges has to
cover the polygons reaching the full 8. Confirmed against build 20260908 at
extent 4096: polygons run to -128..4224, lines to -64..4160.

Its `SURFACE_WATER` method filters out no `kind` — ocean, lake, playa, reef —
because Protomaps' own style paints them all as water. `tunnel` is encoded
only from z14 (`extraAttrMinzoom` in `Water.java`), so below that zoom no
polygon declares itself one. Its `WATERWAYS` fields are what Protomaps sets
on a water *line*; the localized `name:<lang>`, `name2` and `script` variants
ride along on the features themselves, and `kind_detail`, `bridge` and
`tunnel` are polygon-only in this basemap.

Both schemas set `default_extent = 4096`, what every Planetiler-produced tile
encodes its layers at. It is consulted only for a tile with no layer to read
an extent from: a gap tile, or a real tile whose layers all came back empty.

`SCHEMAS` stays a hardcoded dict, mirroring `sources/__init__.py`'s
`SOURCES`. Another schema is one `TileSchema` subclass instance, one
`SchemaName` member and one entry there, with no other code changes.

### Adding a feature set

`features.py` is the one place a feature set's meaning is written down, and
both sides honour it. Adding one for a domain no schema covers yet
(buildings, landuse, POIs) means an entry there plus a `@feature` method on
the schemas that can answer it — never a change to `TileSchema` or `Profile`.

A `FeatureSet` is a name plus what that name *means*, and stays tied to no
schema. `WATERWAYS` says "waterway line features"; every schema able to
answer that declares a `@feature(WATERWAYS)` method, however its own layers
are structured. Identity is object identity, so a schema's `provides` mapping
and a `Tile`'s memo keys use it directly: two feature sets sharing a name are
still two feature sets.

The raw `{"geometry": ..., "properties": ...}` dicts belong to
`mapbox_vector_tile` and stay at its decoder/encoder boundaries. A `@feature`
method returns them as decoded; `Tile.features()` converts to `Feature`, and
`Profile._encode_tile()` converts back. Profile code never deals in them.

## Writing and distributing your own profile

A profile doesn't live in this repository at all, and doesn't need to be
packaged or registered anywhere either: `--profile` is always a path to a
standalone `.py` file, with no separate registration step. There is no
"built-in vs. external" special-casing to be on the wrong side of, because
there is no built-in side:
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
`land.py`/`cropped_waterways.py` are loaded through exactly the path yours
will be, including by this repo's own CI run. The file's one required
export is a module-level `PROFILE` naming your `Profile` subclass (the
class itself, not an instance; `shard_worker.py` instantiates it, with no
arguments):

```python
# my_profile.py
from tilealchemist.profiles.base import Profile

class MyProfile(Profile):
    name = "my-profile"

    def transform(self, tile): ...
    # output_layer_name/mbtiles_name/output_fields()/vector_layers_json()/
    # transform_gap only if their defaults (see "The Profile contract"
    # above) aren't the answer you want

PROFILE = MyProfile
```

`tilealchemist-build-shard --profile ./my_profile.py` loads it straight
from that file via `tilealchemist/profiles/__init__.py`'s `load_profile()`:
`importlib.util.spec_from_file_location()` on the path, then a lookup
for its `PROFILE` attribute, nothing more.

Whatever dependency `MyProfile` itself needs beyond `tilealchemist`'s own
(`pmtiles`, `requests`, `mapbox-vector-tile`, and `shapely`, the last one
because `mvt.py`/`water.py` use it themselves, so a shapely-based profile
like `land` needs nothing extra) is declared in the same file, in a
[PEP 723](https://peps.python.org/pep-0723/) script-metadata block, not in
a `pyproject.toml` or a `requirements.txt`:

```python
# /// script
# dependencies = ["scipy"]
# ///
"""My profile: ..."""
from tilealchemist.profiles.base import Profile
```

`tilealchemist-profile-requirements` reads that block **without importing the
profile**, which is the point of using PEP 723 rather than a module-level
list: the dependencies have to be installed *before* the profile can be
imported at all, so anything that executed the file first would be useless.
It reports a malformed block as a usage error rather than a traceback,
because it runs as one step of a CI install script where the useful output is
which profile is broken and why. An unclosed block is reported rather than
treated as "no dependencies": a profile with one would install none of what
it needs, then fail on the very import the block exists to make possible.

That keeps a profile what `load_profile()` already treats it as: one
self-contained file, with nothing to distribute or keep in sync alongside
it. It also scopes the dependency to the profile rather than to whatever
directory it happens to sit in, so two profiles side by side no longer
install each other's packages.

The block is a comment, so it is read *without* importing the profile,
which is the point: those dependencies have to be installed before the
module can be imported at all, so anything that had to execute the file
first could never work here. `tilealchemist-profile-requirements` does the
reading and prints one requirement per line
(`tilealchemist/profile_requirements.py`):

```
$ tilealchemist-profile-requirements ./my_profile.py
scipy
```

Because it is PEP 723 and not an invention of this project, the same block
is what `uv run --script` and friends already understand.

To run your profile on GitHub Actions without forking this repo, call this
repo's reusable pipeline directly from your own workflow.
`.github/workflows/_pipeline.yml`'s `build-shards` job pipes each
`--profile` path through `tilealchemist-profile-requirements` and pip
installs the result. It does **not** check your repository out: you hand it
your profile as an artifact, uploaded by a job your pipeline call `needs:`
and named by the `profile_artifact` input. The artifact is unpacked at the
workspace root, so a profile at your repo root
is still `./my_profile.py`, and one that never sat in a repository at all,
generated or downloaded by that first job, works exactly the same.
(tilealchemist itself *is* checked out by the pipeline, pinned to the commit
of the workflow file being run, so the package always matches the pipeline.)
This is a native reusable-workflow capability
(`uses: owner/repo/.github/workflows/file.yml@ref` works cross-repo for any
public or shared-private repository), no GitHub Marketplace publishing
needed.
`.github/workflows/_publish-release.yml` is reusable the same way, if a
plain GitHub Release is all the publishing you need:

```yaml
jobs:
  profiles:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/upload-artifact@v4
        with: { name: my-profiles, path: my_profile.py }
  build:
    needs: profiles
    uses: tilelab/tilealchemist/.github/workflows/_pipeline.yml@main
    # `axis_state` is on by default, and its writer pushes with your own
    # token. Without this the build still succeeds; it just keeps the
    # reviewed constants instead of what your last few runs measured. Pass
    # `axis_state: "false"` to turn the whole thing off and drop this.
    permissions:
      contents: write
    with:
      profile: ./my_profile.py
      profile_artifact: my-profiles
      output_basename: my-profile
  publish:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with: { name: my-profile-pmtiles }
      - run: ./publish-wherever-you-want.sh
```

No `attribution` there, and often none anywhere: left off, the layer carries
what the source archive declares for itself. It is a template, so
`attribution: '<a href="https://example.org">&copy; Example</a> {source}'`
puts your own name in front of that, and a value without `{source}` replaces
it outright. It is an input rather than something a profile decides, because
the profile can be somebody else's file while the credit belongs to your run;
see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) "Source attribution".

`_pipeline.yml` only builds and hands you the merged `.pmtiles` as an
artifact; publishing it anywhere (a GitHub Release, B2, elsewhere) is left
to your own workflow, with your own credentials. That split is deliberate,
not an omission; see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) "Why
publishing stops there".

(`profile`/`output_basename` also accept a comma-separated list matched
1:1, e.g. `profile: ./land.py,./cropped_waterways.py` /
`output_basename: land,cropped-waterways`, so several profiles can share
one `prepare-shards` walk and one fetch per worker in a single run instead
of each needing its own `_pipeline.yml` call; see
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
own build workflow, which does exactly this. A single value behaves
identically to calling it with just one profile, as shown above.)

`tilealchemist/sources/__init__.py`'s `SOURCES` and
`tilealchemist/schemas.py`'s `SCHEMAS` stay plain hardcoded name -> class
dicts, unlike `tilealchemist/profiles/__init__.py`'s path-based
`load_profile()`: a new source or schema is a small, self-contained
addition this repo's own maintainers are expected to add directly, not the
point of external extensibility here.
