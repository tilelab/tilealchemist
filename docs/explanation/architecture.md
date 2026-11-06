# How convert works

How `tilealchemist convert` runs, splits the work and uses memory, and what
it leaves to others. Its goals: decode each distinct tile once for all
profiles, use every core, keep memory independent of the archive's size,
fail before the first tile where possible, and write standard PMTiles.
Details of a single mechanism are in code comments next to it.

## The phases of a run

`convert()` in `conversion/convert.py`:

1. Load the profiles and check the layer names (`profiles/loader.py`,
   `config/run_config.py`).
2. Open the archive: memory-map it, read the header and root directory, and
   narrow the zoom range (`archive/local.py`).
3. Compose the attribution and check the schema (`archive/attribution.py`,
   `archive/schema_check.py`).
4. Walk the directory into entries in offset order (`archive/directory.py`).
5. Transform chunks of entries on a process pool
   (`conversion/transform_pool.py`, `conversion/transform_chunk.py`).
6. Write each layer: record the output as chunks arrive, then sort it by
   tile ID and rename the file into place (`archive/layer_writer.py`).

## How the work is split

- An **entry** maps a run of tile IDs to a blob. Offset order puts a blob's
  entries together, so the blob is decoded once, at its first entry, and
  the output reused for the rest.
- `conversion/partition.py` cuts the entries into chunks of equal blob
  bytes, never splitting a blob. A blob's cost grows with its size, from
  about 100 B of ocean to 300 KB of city, while duplicates cost almost
  nothing.
- There is one process per core and several chunks per process
  (`CHUNKS_PER_PROCESS`), so a process that finishes early takes the next.
- Processes get the profiles' paths and the schema's name and load them
  themselves, so the pool works with both fork and spawn.

## Memory and disk

- The archive is memory-mapped.
- Only one chunk per process plus a small lead (`SUBMIT_LEAD`) is in
  flight, and its output goes straight to the layer writers.
- A `LayerWriter` keeps a few numbers per tile run in memory and spools each
  distinct output tile to disk once. Gzip with a fixed timestamp makes
  identical tiles identical bytes.
- Finishing a layer needs about twice its size on disk.

## What convert leaves to others

- Gaps: tilealchemist-pipeline builds them, by pointing them at an empty
  tile in the archives it hands `convert`.
- The pipeline: tilealchemist never imports it and finds its `distributed`
  command as a `tilealchemist.commands` entry point. The pipeline imports
  only the run's contract, the checks it repeats to fail early, and PMTiles
  helpers the core uses itself.
- GitHub: CI log formatting comes in through environment variables, and
  `lint.yml` rejects `GITHUB_` and `api.github.com` in the package.

## Where the code lives

| Package | Contents |
| --- | --- |
| `cli/` | The `tilealchemist` command; each subcommand parses, then calls its module. |
| `conversion/` | `convert`: chunks and the process pool. |
| `archive/` | PMTiles on disk: archive, directory, layer writer, attribution, schema check. |
| `profiles/` | `Profile`, `Tile`, features, MVT and water helpers, loading profiles. |
| `schemas/` | `TileSchema` and the shipped schemas. |
| `config/` | The run config, plugin lookup, the installer behind `setup`. |
| `console/` | Warnings, log groups and progress lines. |
