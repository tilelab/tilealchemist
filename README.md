# TileAlchemist

A pipeline for building global PMTiles layers from an existing
OpenStreetMap-based PMTiles source, tile by tile, driven by pluggable
**profiles**: a profile decides what each tile becomes. Fetching, sharding
(128-way by default, configurable), and merging are the pipeline's job; a
profile is just the per-tile transform plugged into it.

## Profiles

This repository ships the pipeline, not any particular profile. A profile
is a standalone `.py` file living in whichever repository wants it, pointed
at by path; there is no registration step and no built-in/external
distinction. See [`docs/PROFILES.md`](docs/PROFILES.md) to write one.

For two real ones, plus the finished world-covering PMTiles layers they
produce and a MapLibre style for each, see
**[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)**:

| Profile | Produces |
| --- | --- |
| `land` | Every tile's land polygon(s), derived by inverting the source's `water` layer. |
| `cropped-waterways` | The source's `waterway` lines, cropped to the portions that don't overlap real water. |

That repository is also the worked example of the cross-repo path: it calls
this repo's `_pipeline.yml` and `_publish-release.yml` from its own
workflow, exactly the way any other adopter would.

## Running it

Everything runs on GitHub Actions; there is nothing to install to trigger a
build. A caller workflow calls `_pipeline.yml` (see
[`docs/PROFILES.md`](docs/PROFILES.md) for a complete one), which walks the
source archive once, fans out to `worker_count` parallel workers (128 by
default, or `auto` to size the run against its own budgets), and merges
each profile's shards into its own `.pmtiles`
artifact. `profile`/`output_basename` accept a comma-separated list, so one
run can build several profiles off a single `prepare-shards` walk and a
single fetch per worker. OpenFreeMap sees each tile's bytes fetched once
per run regardless of how many profiles are built from them.

`_pipeline.yml` deliberately publishes nothing. Publishing needs
credentials, and credentials belong to the repository that owns them; see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) "Why publishing stops there".
`_publish-release.yml` is offered as a reusable workflow for the one case
that needs no credential of its own, a GitHub Release under the caller's
own token.

`pyproject.toml` and the `tilealchemist/` package below are what you read
if you're changing or extending the pipeline itself.

## Architecture at a glance

Three small abstractions keep the pipeline generic: a **`Profile`** decides
what each tile turns into, a **`Source`** decides where the input PMTiles
archive comes from, and a **`TileSchema`** decides what its layers/attributes
are actually called.

What ships today: the `openfreemap` and `protomaps` sources (plus
`static-url` for a fixed URL of your own), and the `openmaptiles` and
`protomaps` schemas. **A source names its own schema**, so picking
`--source protomaps` is the whole decision — there is no second flag to get
wrong, and no way to read a Protomaps build as OpenMapTiles by accident.
`--source static-url` is the one exception, a bare URL being nobody's
provider in particular: it requires `--schema`, and every other source
refuses one rather than quietly ignoring it.

- [`docs/PROFILES.md`](docs/PROFILES.md): the `Profile`/`TileSchema`
  contracts and how to write and distribute a profile of your own.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): `Source` resolution,
  batched fetching, configurable sharding, publishing, and the GitHub
  Actions structure.

## Repository layout

The two documents above cover the `docs/` directory; this is everything
else.

| Path | Purpose |
| --- | --- |
| `.github/workflows/_pipeline.yml` | Reusable: prepares shards, builds them in parallel, merges into one `.pmtiles` artifact per profile. Never publishes. Safe to call cross-repo. |
| `.github/workflows/_publish-release.yml` | Reusable: publishes a merged `.pmtiles` artifact as a GitHub Release. Safe to call cross-repo. |
| `.github/workflows/test.yml` | CI: real low-zoom runs against live OpenFreeMap *and* Protomaps data, built with [tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)' profiles, on every push and PR. |
| `pyproject.toml` | Packaging: dependencies and console scripts. A profile needing anything beyond these declares it inline, in a PEP 723 block in its own `.py` file. |
| `tilealchemist/prepare_shards.py` | The run's entry point: parses its flags, then hands off to `shard_prep.py`. |
| `tilealchemist/shard_prep.py` | The run's one-time planning step: resolves the `Source`, drives the walk and the partition, writes the manifests, logs the run. Needs a `Source`, not a `Profile`. |
| `tilealchemist/attribution.py` | What a built layer credits: reads the attribution the source archive declares for itself, and fills the caller's `{source}` template in with it. |
| `tilealchemist/pmtiles_index.py` | The source archive's directory index: header + root, then only the leaf directories the zoom range needs, in two range requests, walked and zoom-pruned in memory into directory entries. |
| `tilealchemist/partition.py` | Those entries (plus the gaps between them) into one balanced, contiguous block of work per worker, inside the hard caps. No network, no files. |
| `tilealchemist/budgets.py` | The caps themselves: how many manifest records a worker may hold and how big its largest range request may get. |
| `tilealchemist/sizing.py` | Picks `worker_count` itself (`--worker-count auto`): the smallest multiple of the concurrency whose worst worker fits the time, RAM and disk budgets. |
| `tilealchemist/calibration.py` | Fits the next run's cost coefficients from the last run's `usage:` lines, with the guards that stop a bad fit being adopted. |
| `tilealchemist/calibrate.py` | CLI for that: prints what it would change and, with `--out`, writes a `calibration.json`. It never edits `cost.py`. |
| `tilealchemist/build_shard.py` | One worker's entry point: parses its flags, then hands off to `shard_worker.py`. |
| `tilealchemist/shard_worker.py` | One worker's control flow: fetches its manifest's tiles in a single range request per contiguous run of them, drives the transform and the shard writing, logs the run. |
| `tilealchemist/fetch_batching.py` | Groups a worker's manifest entries into range-GET batches (split at wide unread gaps) and fetches one batch's bytes. |
| `tilealchemist/transform.py` | Fetched bytes to output tiles: one decode per tile shared by every selected `Profile`, timed apart from the per-profile transform. |
| `tilealchemist/transform_pool.py` | Splits a batch into cost-balanced chunks and runs them across this machine's cores (`--transform-workers`), throttling how many are in flight. |
| `tilealchemist/usage.py` | What the run actually cost: per-chunk and per-worker `usage:` lines (seconds by phase, bytes, distinct blobs, peak RSS, free disk). |
| `tilealchemist/mbtiles.py` | The shard files themselves: creating one mbtiles per profile in either layout (`--shard-layout`), expanding runs into rows (including the XYZ-to-TMS row flip). |
| `tilealchemist/profile_requirements.py` | Reads a profile's inline PEP 723 dependency block without importing it, so CI can install what the profile needs before loading it. |
| `tilealchemist/profiles/` | The `Profile` ABC and the path-based `load_profile()`. No profiles: those live in their own repositories. |
| `tilealchemist/sources/` | The `Source` ABC (which archive URL to read, and which schema its tiles are in), plus `OpenFreeMapSource`, `ProtomapsSource` and `StaticUrlSource`. |
| `tilealchemist/schemas.py` | The `TileSchema` ABC, its `@feature` feature sets, `OpenMapTilesSchema` and `ProtomapsSchema`, and the `SchemaName` enum / `SCHEMAS` registry. |
| `tilealchemist/zoom.py` | The `ZoomLevel` enum (the levels a run can be walked at) and the `MAX_SUPPORTED_ZOOM` limit behind it. |
| `tilealchemist/features.py` | The vocabulary a profile works in: `Feature` (shapely geometry + properties) and the `FeatureSet` constants a profile asks a schema for. |
| `tilealchemist/tile.py` | The `Tile` a profile's `transform()` is handed: decoded layers, extent, schema feature sets, per-tile memoization. |
| `tilealchemist/mvt.py` | Gzip+MVT decode/encode helper any profile can use, including output grid snapping. |
| `tilealchemist/water.py` | Geometry math offered to water-related profiles; used by no other module here, only by profiles that ask for it. |
| `tilealchemist/manifest.py` | Both sides of everything `shard_prep.py` hands the workers: the binary per-worker manifest format, and the shared `source.json` (as the `SourceMetadata` record a worker reads it back into). |
| `tilealchemist/ranged_fetch.py` | HTTP Range fetching against the source archive (session, retry/backoff, 206 enforcement, download progress), shared by `pmtiles_index.py`/`fetch_batching.py`. |
| `tilealchemist/backoff.py`, `tilealchemist/throttle.py`, `tilealchemist/throttle_progress.sh` | HTTP retry backoff, throttled progress logging. |
| `lint/comment_style.py` | The comment-style linter CI runs; see [`docs/COMMENT_STYLE.md`](docs/COMMENT_STYLE.md). |

## Related projects

- **[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)**
  holds the `land` and `cropped-waterways` profiles, the workflow that
  builds and publishes them monthly, and the finished PMTiles layers. It is
  a consumer of this project, and the reference for how to call
  `_pipeline.yml` from your own repository.
- **[TileDistillery](https://github.com/foxandfeature/tiledistillery)** is a
  sibling pipeline solving the adjacent problem: PMTiles layers built from
  raw [Geofabrik](https://download.geofabrik.de) OSM extracts with
  [tilemaker](https://github.com/systemed/tilemaker), region by region,
  rather than reshaped from an existing PMTiles archive. It is independent
  of this one; neither is input or output for the other. See
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) "Worker independence" for
  where the two diverge and why.

## Contributing

Bug reports and pull requests are welcome.

Code here is self-documenting, and comments are held to a strict, linted
style: one line each, attached to the code they comment on, at most five per
file counting docstrings, with the reasoning behind a design living in
`docs/` instead. See [`docs/COMMENT_STYLE.md`](docs/COMMENT_STYLE.md) for the
rules and the rationale. CI enforces them; run the check yourself with:

```sh
python3 lint/comment_style.py tilealchemist lint
```

## License / attribution

The code in this repository (package, workflows) is licensed under
the [MIT License](LICENSE).

The `.pmtiles` files themselves are a different matter: their data is
derived from OpenStreetMap, via OpenFreeMap's planet archive built with
OpenMapTiles or via Protomaps' daily basemap builds (© OpenStreetMap
contributors, ODbL either way), and that license carries through however
it's reshaped or repackaged downstream. Both sources publish their archives
as ODbL Produced Works requiring visible `© OpenStreetMap` attribution, so
a layer built from either is under the same obligation; a Protomaps build
additionally carries public-domain Natural Earth data and, in its
`landcover` layer only, CC-BY-4.0 data that would need its own attribution
if a profile ever read that layer (none here does). Each layer carries
its attribution in its own PMTiles metadata, so a style reading it through
the [PMTiles protocol](https://github.com/protomaps/PMTiles) picks it up
automatically. That string is not typed out in full in a workflow file: the
pipeline reads what the source archive declares and fills it into the
`attribution` template, so a run credits the provider whose bytes it actually
read. A run that cannot state what its output credits fails instead of
publishing an unattributed layer (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) "Source attribution"). See
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)
for the layers built from this pipeline today.
