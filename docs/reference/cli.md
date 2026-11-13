# Command-line reference

`tilealchemist --version` prints the version, and `-h` prints help for any
command. Exit status: `0` on success, `1` when a run fails, `2` for invalid
arguments or an invalid run config.

## tilealchemist setup

```sh
tilealchemist setup CONFIG
```

Installs the [run config](run-config.md)'s `plugins`, then the dependencies
each profile declares in its PEP 723 block, into the environment
tilealchemist runs in. It uses pip, or `uv pip` where the environment has no
pip, and pins tilealchemist to the installed version. It reads profiles as
text and never imports them. Running it again is safe.

## tilealchemist convert

```sh
tilealchemist convert ARCHIVE CONFIG [OPTIONS]
```

Builds every layer of a run from `ARCHIVE`, a local PMTiles v3 file of
gzipped Mapbox Vector Tiles (MVT).

| Option | Default | Description |
| --- | --- | --- |
| `--min-zoom N`, `--max-zoom N` | The run config's | The zoom range to build. |
| `--transform-processes N` | One per CPU core | Processes that transform tiles. `1` runs the profiles in the main process. |
| `-o DIR`, `--output-dir DIR` | `.` | Where the layer files go. |
| `--overwrite` | Off | Replace existing layer files. |

An empty value counts as not given. Before the first tile, `convert` fails
if:

- the run config is invalid, or its schema isn't installed;
- a profile can't be loaded, two profiles share a `name`, or a layer file
  exists without `--overwrite`;
- the archive isn't gzipped MVT, is truncated, or holds none of the run's
  zoom levels;
- the [attribution](run-config.md#attribution) can't be composed, or the
  archive fails the [schema check](schemas.md#the-schema-check).

An exception in a profile stops the run, and no layer file is written:

```
RuntimeError: profile 'land' failed on tile 9/269/151
```

## Layer files

`convert` writes `<output dir>/<name>.pmtiles` per profile.
tilealchemist-pipeline merges layers built on different machines and relies
on this format:

- PMTiles v3 of gzipped MVT, clustered: tile data in tile ID order, each
  distinct tile stored once.
- Written as `<name>.pmtiles.tmp` and renamed when complete. A layer without
  tiles gets no file, and `--overwrite` deletes an old one.
- Header: the zoom range of the tiles written; bounds and center from the
  archive, the center zoom clamped into the layer's range.
- Metadata: `name` (the profile's `name`), `format` (`"pbf"`), `minzoom`
  and `maxzoom` (the run's range narrowed to the archive's, as strings),
  `attribution`, and `vector_layers` (the profile's `vector_layers()`).

## Log output

The log goes to stderr: status lines such as `layer: ./land.pmtiles`,
progress lines starting with `update:` (at most one a minute per chunk),
warnings, and groups that fold long listings. Environment variables format
warnings and groups for a CI system:

| Variable | Default |
| --- | --- |
| `TILEALCHEMIST_LOG_WARNING` | `warning: {title}: {message}` |
| `TILEALCHEMIST_LOG_GROUP` | `{title}:` |
| `TILEALCHEMIST_LOG_END_GROUP` | Empty, which writes no line. |

For GitHub Actions annotations and folded groups:

```yaml
env:
  TILEALCHEMIST_LOG_WARNING: "::warning title={title}::{message}"
  TILEALCHEMIST_LOG_GROUP: "::group::{title}"
  TILEALCHEMIST_LOG_END_GROUP: "::endgroup::"
```
