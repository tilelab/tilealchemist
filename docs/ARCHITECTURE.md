# Architecture

How the pipeline works *around* a profile: resolving the source archive,
fetching it, sharding the work across workers, and publishing the result.
For what a profile actually computes, see
[`docs/PROFILES.md`](PROFILES.md) (the system) and
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)
(a worked example: the `land`/`cropped-waterways` layers).

## Source resolution

`Source.resolve()` (`tilealchemist/sources/`) finds the PMTiles archive URL
to read from, independent of which profile is running, and says which schema
that archive's tiles are in.

Both, not just the URL. A provider publishes one schema, not a different one
per build, so which schema to decode through is the source's own property
rather than a second thing a caller has to get right alongside it: `--source
protomaps` cannot be read as OpenMapTiles by accident, because there is no
flag left with which to say so. A source declares the `TileSchema` itself,
not a name for it, so a declaration cannot be misspelled into a lookup that
fails somewhere downstream; the name it travels under appears once it has
to cross a file, in `source.json` (`manifest.py`), which is also what keeps
a worker from disagreeing with the walk about it. That closes a failure that is otherwise silent rather
than loud: both schemas here have a `water` layer, so a run pairing one
provider's archive with the other's schema doesn't crash, it produces
plausible nonsense — OpenMapTiles' reader would take a Protomaps tile's
waterway lines and label points for water polygons and filter them on a
`brunnel` attribute that isn't there.

`OpenFreeMapSource.resolve()` (the default) re-resolves on every run:
`files.txt` is scanned for the newest `areas/planet/<timestamp>_pt/`
directory that has **both** a `done` marker and a `tiles.pmtiles` file (the
very newest directory listed isn't necessarily done converting yet).

`ProtomapsSource` (`--source protomaps`) re-resolves the same way against a
different shape of listing: `build-metadata.protomaps.dev/builds.json` names
every build in the bucket, and the newest build *date* wins, not the newest
entry, because the index also keeps the last build of each older basemap
version around. It needs no equivalent of the `done` marker: the index is
generated from the bucket's object listing, where an object appears only
once its upload has finished. Re-resolving every run is not optional here
the way it nearly is for OpenFreeMap, since Protomaps keeps only the last
seven days of daily builds, so any build named in a workflow file would be
a 404 within the week.

`StaticUrlSource` (`--source static-url --source-url ...`) skips that
resolution for any other PMTiles provider that just publishes one file at a
stable location. It is the one source that cannot name its own schema, a
bare URL being nobody's provider in particular, so it is the one that takes
`--schema` (the pipeline's `schema` input) and requires it. Passing one to
any other source is refused rather than ignored: a `--schema` that
contradicts what the source publishes is a misunderstanding worth stopping
for, and one that agrees is merely redundant. It is also the way to read a copy of your own, which is
what Protomaps asks for if a *deployed* map is what would be reading it
(docs.protomaps.com/basemaps/downloads discourages hotlinking their builds,
whose URLs can move); one archive walk per build run is the download that
page describes, not the serving it warns about.

## Source attribution

What a layer credits is a statement about the data it was built from, so the
pipeline reads it off the archive that was walked rather than taking the
caller's word for it. PMTiles v3 keeps a JSON metadata document between the
root directory and the tile data (spec section 4), and both sources this
repository ships state an `attribution` in it. They do not state the same one:

    OpenFreeMap   OpenFreeMap, &copy; OpenMapTiles, &copy; OpenStreetMap contributors
    Protomaps     &copy; OpenStreetMap

(anchors in the archives, flattened here). A constant in a workflow file
cannot follow that difference. Switching `source` leaves the old string in
place and nothing in the run disagrees: the layer publishes, crediting a
provider whose bytes it never read.

`attribution.py` reads it during `prepare-shards`, for one ranged request of
about a kilobyte. The metadata lies outside the 16 KB prefix
`pmtiles_index.py` already holds — OpenFreeMap writes it just past that
boundary, Protomaps at the end of a 137 GB archive — so it is cheap rather
than free.

What the run makes of it is the caller's business. The `attribution` input is
a template in which `{source}` stands for what the archive declared:

| `attribution` | the layer credits |
| --- | --- |
| unset | the archive's own attribution, unchanged |
| `<a ...>&copy; Example</a> {source}` | the caller's name, then the archive's |
| `&copy; Example` | that, instead of the archive's |

This belongs to whoever runs the pipeline rather than to the profile. A
profile can be a file downloaded from anywhere, and editing it to add a name
is not something a caller should have to do; the credit is a property of the
run, not of the transform. It is also why one answer serves every profile in a
run: `prepare-shards` resolves the source, walks it and reads its metadata
without importing caller code at all.

The substitution happens in `compose_attribution()`, not in the workflow's
shell, and that is load-bearing: bash's `${v//p/r}` expands an unescaped `&`
in the replacement to the matched text, and both sources' attributions are
full of `&copy;`, so composing there would corrupt exactly the strings this
exists to carry.

An empty attribution is refused rather than published. An archive declaring
none with no template to stand in for it, a template whose `{source}` has
nothing to fill it with, and a template that composes to nothing all fail the
run inside `prepare-shards`, before a worker is dispatched. `merge` asserts
the value once more before `tile-join` stamps it, the value having crossed a
job output in between.

## Fetching: directory-driven, not one request per tile

A naive implementation would issue one HTTP range request per tile: at
z0..z14 that's ~358M individual requests, too much load for a single free
community-run server. Instead:

1. `tilealchemist/prepare_shards.py` (the entry point; `shard_prep.py` for
   the run's flow, `pmtiles_index.py` for the walk and the gaps it leaves,
   `partition.py` for the split that follows) walks the PMTiles directory tree (root +
   leaf directories) between `min_zoom` (default 0) and `max_zoom` **once,
   for the whole run**, not once per worker, yielding every tile's
   `(tile_id, offset, length, run_length)`.
   Depends only on the resolved `Source`, never touches tile content, so
   it's identical regardless of which `Profile` runs later. Both bounds
   prune the walk itself, not just its result: sibling entries in a
   directory are sorted and non-overlapping tile-ID ranges, so a child
   pointer whose whole range falls outside `[min_zoom, max_zoom]` is never
   even decoded, the same way `max_zoom` already skipped subtrees entirely
   past it before `min_zoom` existed (see `pmtiles_index.py`'s
   `walk_directory_tree()`).

   The walk itself issues no requests at all, and only the part of the
   index it will actually read comes down. `collect_entries()` fetches a
   16 KB prefix holding the 127-byte header and the root directory, works
   out from the root alone which leaf directories the zoom range needs, and
   fetches exactly that span: two requests for the entire run. 16 KB is not
   a guess -- PMTiles v3 (spec section 4) requires header plus root to fit
   in the first 16,384 bytes precisely so one blind fetch can get them, so
   the root never needs a request of its own. The pruning matters: a planet
   archive's leaf section is ~96 MB, while a z0..z5 run needs ~29 KB of it.

   The span is the first to the last leaf the root points into, which relies
   on two things the same spec section asks of writers: leaf order SHOULD
   ascend by tile-ID, and more than one level of leaf directories is
   discouraged. Neither is a MUST, so both are checked rather than trusted --
   `LeafWindow.node_bytes()` raises on any pointer outside the span instead
   of slicing whatever bytes sit there. Reading such an archive would mean
   pulling the whole leaf section, and no published archive is built that
   way (verified against OpenFreeMap and Protomaps planet builds, whose root
   pointers tile their leaf section back-to-back with no gaps), so it errors
   out rather than carrying a fallback path that never runs.

   Nor does it rest on where in the file that section *is*: the header says,
   and writers disagree. OpenFreeMap's archives run
   Header/Root/Metadata/LeafDirs/TileData, Protomaps' daily builds put the
   leaf directories and metadata *after* 137 GB of tile data, and both read
   identically here because every offset comes out of the header rather than
   out of an assumed order. A run reads 29 KB of OpenFreeMap's index and
   132 KB of a Protomaps build's for z0..z4.
2. It sorts entries by *offset* (not tile-ID) and splits them into
   **contiguous** chunks, one per worker, at a count it picks itself (see
   "Sizing a run") (`tilealchemist/partition.py`,
   written out by `tilealchemist/manifest.py`). Offset order tracks tile-ID
   order almost everywhere, but also catches what tile-ID order misses: an
   entry that dedupes against a *non-adjacent* tile with identical bytes
   (e.g. the same "all water" tile recurring across different oceans) lands
   in the same worker as the tile it's deduped against, instead of a random
   other worker re-fetching the same bytes. `partition.py`'s
   `partition_by_cost()` cuts at the record where a worker's share ends,
   through a run of same-offset entries or not (see "Parallelism" for what a
   share weighs), so every block lands within one record of its share. A cut
   through such a run costs one tile fetched and transformed again by the
   next worker. Keeping runs whole was tried and cost far more: a Protomaps
   planet build dedupes its open ocean into same-offset runs of millions of
   entries, chopped into chunks of one whole share each, and a chunk landed
   on a worker in one piece whatever it already held. Standardprofiles run
   36608758128 handed worker 0 a 2660s chunk on top of the 1081s it had,
   63m predicted against an even 45m, and the offset carried through the
   next three workers until worker 4 was left with 18m.
3. Each worker (`tilealchemist/build_shard.py` for the entry point,
   `shard_worker.py` for the run's flow, `fetch_batching.py` for splitting
   and fetching its manifest, `transform.py` for the CPU-bound transform,
   `mbtiles.py` for the shard files it writes) reads only its own manifest
   and fetches it with a single range GET spanning its first entry's offset
   to its last entry's end, the manifest already being a contiguous slice
   of offset order.

   Contiguous in *entries*, though, not necessarily in bytes: dedup (step 4)
   points an entry at whatever tile first held those bytes, so a run that
   starts at a high `--min-zoom` still has entries deduped against tiles
   below it, sitting far back in the file among data the run never reads.
   Two neighbouring entries can then be gigabytes apart and one GET across
   them would download all of it, so `plan_fetch_batches()` splits the
   manifest at every gap wider than `--max-fetch-gap` (8 MB by default) and
   the worker fetches, transforms and drops one batch at a time. Narrower
   gaps are fetched through, a few MB of unread bytes on an open, streaming
   connection being cheaper than another round trip against a cold CDN.

   When a worker is building multiple profiles in one invocation (a
   comma-separated `--profile` list, see "Publishing" below), it still does
   exactly one range GET per batch and reuses those same fetched bytes for
   every profile's transform, instead of fetching once per profile.
4. Reuses PMTiles' own deduplication: byte-identical tiles (e.g. a long run
   of open-ocean tiles) share one directory entry with a `run_length`,
   decoded and transformed once. Non-adjacent duplicates (step 2) show up
   as two separate entries at the same `(offset, length)`, landing next to
   each other once sorted, so `transform.py` memoizes on
   `(offset, length)` within a batch instead of re-decoding.
5. Accounts for *gaps*: tile-IDs with no directory entry at all (OpenFreeMap
   only stores a tile if it has something to render, so large empty
   stretches like desert or ice sheet interiors are simply absent).
   `pmtiles_index.py`'s `compute_gaps()` finds and chunks these, tagged with a
   sentinel `length=0` so the worker asks each profile for `transform_gap`
   once and writes that at every one of their coordinates instead of
   fetching anything (see `shard_worker.py`'s `run_worker()` and
   `mbtiles.py`'s `write_gap_tiles()`).

### Worker logging

A worker's stderr is two kinds of line. **Major lines** always print, one
per phase transition, never more: source resolved, entries assigned,
`starting download`/`starting transform`, each chunk's `chunk N done`, and
the final written/skipped summary. **Update lines** (`update: ...`) exist
only so a step that is taking a while doesn't look stuck, and are throttled per
phase: transform progress at most once every 60s (`--report-interval`), per
transforming process and over its own chunk when the transform is pooled;
download progress at most once every 15s (`--download-report-interval`).
Neither prints at all if its step finishes before the first interval
elapses, so a fast worker's whole log is major lines only.

A third kind, **`usage:` lines**, carries measurements rather than progress
and is meant to be read by machine; see "Measuring a run" below.

Download progress gets its own flag, and its own shorter default, rather
than sharing `--report-interval`, because the phase it covers is itself
shorter: a shard's whole-batch download is a single Range request of tens to
low hundreds of MB and routinely finishes well under 60s on a healthy
connection, so at the transform's interval it would print nothing at all,
while the transform right after it, with many chunks each logging a major
`chunk N done` line as they complete, looks much more alive by comparison.

## Parallelism

Splitting the work into many small per-worker jobs rather than fewer big
ones costs almost nothing (installing tilealchemist is seconds), and keeps
each job far from GitHub Actions' 6-hour per-job runtime limit, plus
smaller, failure-isolated jobs and fewer, smaller range requests. GitHub
also queues anything past ~20 concurrently *running* jobs on a public repo,
so a higher `worker_count` doesn't add parallelism at any one moment, just
keeps each job smaller -- and lets a lane on a fast runner take over more of
them: that's why the sizing starts at a small multiple of that concurrency,
`--worker-scale` times it, and only ever considers multiples of it rather
than reaching for a large count outright (see "Sizing a run"). Each worker writes its own small mbtiles
shard; a final job merges all shards
with `tile-join` into one `.pmtiles` file.

A worker building multiple profiles in one run (`_pipeline.yml` called with
a comma-separated `profile`) still does exactly one fetch, so per-worker
*network* time is unchanged. Per-worker *CPU* time doesn't simply scale
with the number of profiles either: `transform.py` decodes each unique tile
once into a single `Tile` object (see `tilealchemist/tile.py`) and hands
that same object to every profile back-to-back (entries outer, profiles
inner in `transform_batch_blob_multi()`). Anything derived from a tile (its
decoded layers, a schema feature set, `water.py`'s
`surface_water_union()`) is memoized on that object, so a second profile
reading the same tile gets the first one's result rather than repeating the
gunzip+protobuf decode or the polygon-union work. The `Tile` is dropped
when the entry is done, which bounds that sharing's memory to one tile at a
time. Two profiles that don't share any of that underlying work (a
hypothetical buildings profile alongside `land`, say) still each pay their
own cost in full: only genuinely-shared steps (decode, and a water profile
pair's own shared water union) come out cheaper.

Independently of that, the transform phase itself (decode, transform,
encode, sqlite insert, all of them CPU-bound rather than network) fans out
across a worker's own CPU cores via `--transform-workers` (default: all
available cores; `1` disables pooling and runs everything in the worker
process).
`real_entries` is split into contiguous chunks by `partition.py`'s
`partition_by_cost()`, the same function that splits a run's gaps across
workers (its real entries go by whole tile blocks; see "Measured tile
blocks"), deliberately producing several times more chunks than there are processes
(`TRANSFORM_CHUNKS_PER_WORKER`). Both halves of that matter.

### What a record costs

`cost.py` answers that, and `partition_by_cost()` splits on cumulative cost
rather than on a record count. `AXIS_SECONDS` holds five coefficients, all
in seconds, and a record's cost is their terms added up:

- **Per decode call** and **per byte decoded**, the pair that dominates.
  Both are paid once per *distinct* entry — a record repeating the previous
  one's `(offset, length)` pays neither, matching what
  `transform_batch_blob_multi()` actually does. The byte term grows *faster*
  than byte length, a bigger tile also being a denser one: more features to
  decode, more geometry for a profile to clip and union.
- **Per byte fetched**, also once per distinct entry: the range GET is real
  time, and bytes are what bound a worker's peak blob memory.
- **Per record**, at its measured cost; the memory bound it used to stand in
  for is nothing now (see "Why no budget caps a worker").
- **Per output tile**, one sqlite insert per tile per profile.

Two things scale those terms rather than adding to them, and both are
corrections the model needed rather than refinements it wanted (see "Two units
bugs, and what they cost").

**The decode and the profiles' seconds are divided by
`transform_parallelism`, and the fetch and the write are not.** That split is
the one the worker itself makes: `run_transform()` fans a batch out across
`--transform-workers` processes, so `length_hist` and a profile's
`transform_seconds` come back *summed across the pool*, while
`fetch_batch_blob()` and `ShardWriter.write()` run in the worker, one batch at
a time. Charging a summed figure to a wall clock overstates it by whatever the
pool bought.

**The write is charged on bytes.** Where the record's tile block has been
measured, on the bytes that block wrote last time (see "Measured tile
blocks"). Where it has not, on the profile's declared `bytes_per_output_tile ×
written_share`: a profile is handed every tile in the run and writes only the
ones it has something to say about, and `bytes_per_output_tile` is the weight
of a tile it *wrote*, so charging it on every tile in a record bills an ocean
for a coastline that is not in it.

`cost_weights()` therefore returns a predicted *duration* rather than a
share of something: `partition_by_cost()` splits on that number, and
`prepare_shards` prints the run's predicted core-hours and slowest worker, plus
one line per manifest — `worker-NNN: 12.3m predicted, ... 9% of budget` — in a
folded `::group::`, so the shard that overran can be read back against what it
was predicted to cost, and an uneven partition is visible as a spread rather
than only as its worst cell.

**All of it travels as one `CostModel`** — the axes, the settled profile costs
and the parallelism in a single tuple — because the alternative was tried and
failed quietly. `partition_by_cost()` used to take the axes alone while
`worst_load()` took the axes *and* the profile costs, so a run was split by one
model and judged by another, and nothing in either signature said so.

The coefficients are fixed rather than recomputed per run, which is the
correction to an earlier version of this model. That one normalized each
axis to its own total across the run and mixed the results by fixed shares
(0.25 / 0.60 / 0.15). Algebraically that is a linear combination of the
same kind, but with a decode coefficient of 0.60/W against the run's total
decode work W -- so the more decode-bound a run was, the less
each byte of decode counted, which is backwards. What the shares really
encoded was "every run has the composition of the planet run they were
fitted against". That holds for planet builds and breaks either way for
anything else: a z0..z11 ocean-heavy range is almost all sqlite inserts
against one decode per deduped run, a high-zoom extract is almost all
decode, and the normalized model insisted both were 60% decode.

Balancing by a *single* axis was tried in production before either version
and failed badly in opposite directions. Balancing on bytes alone let one
real run hand a worker 3.5M real entries against its peers' 500K-900K, a
near-identical download size at ~5x the decode work, which ran that worker
out of memory. Balancing on `run_length` alone was worse: a handful of
entries with a huge `run_length` "fills" a tile-sized target almost
immediately while costing almost no decode work, so regions dense in those
pushed every real, unique-content entry (`run_length` 1, one decode each,
and just as many bytes to fetch) onto whatever workers were left, producing
a 4.3M-entry/16GB worker next to a 76K-entry/7MB one.

Both are degenerate cases of this model, with all but one coefficient at
zero, and that is also what bounds it: with every coefficient strictly
positive, a worker can exceed its fair share of any one axis only by that
axis's reciprocal share of the run's total cost, so none can run away.
Normalizing made that bound a fixed constant; absolute coefficients let it
track what the run is actually made of, which is the point.

### Where the coefficients come from

`AXIS_SECONDS` is fitted against the shard manifests and the 128 measured
worker durations of the 2026-09-19 `land` + `cropped_waterways` planet run
(standardprofiles run 35447809178): 58,679,705 records, 43,272,366 distinct
decode calls, 357,913,942 output tiles, no gap ranges at all, 15.7
core-hours, workers between 39s and 34m12s. The coefficients are scaled so
the model's total matches that run's measured work, which is what makes the
printed core-hours figure mean anything; only the ratios between them
affect partitioning.

Two single-worker observations from that run set the small coefficients
directly, without a regression:

- One worker held 1,449,554 records whose `(offset, length)` deduped down
  to **one** distinct entry. It finished in 39s -- the floor across all 128,
  i.e. job setup and nothing else. A record that decodes nothing therefore
  costs single-digit microseconds, not the 25% of a worker's budget the
  normalized model gave it.
- Another wrote 16,569,677 output tiles in 49s total. That bounds one
  sqlite insert at well under a microsecond, against the 15% share the
  normalized model spent on it. `run_length` inserts write the same deduped
  blob over and over, and sqlite does that almost for free.

Both are why `real_tiles` correlates *negatively* (-0.43) with duration
across the 128 workers: a tile-heavy worker is an ocean-heavy worker, which
is a cheap one.

Judged on how well each weighting ranks the workers this run actually had,
the normalized model scores a Pearson correlation of **0.049** against
measured duration (Spearman 0.059) -- it is, for practical purposes,
uncorrelated with what the workers went on to do. The fitted coefficients
score 0.538 (Spearman 0.644).

### Two units bugs, and what they cost

Standardprofiles run 36573352744, a `land` + `cropped_waterways` planet build
on 160 workers, was predicted at **61.6 core-hours and measured 15.9** — 3.88x
over on the total, while *under*-predicting its own slowest workers. Both
halves of that were one kind of mistake: a coefficient measured in one unit and
spent in another.

**The pooled seconds.** 38.9 of the 61.6 predicted core-hours were the
profiles' `seconds_per_tile`, and the run's profile rows report 38.4
core-hours of `transform_seconds` — the prediction was almost exactly right.
It was right about *CPU*. Those seconds are measured inside the pool processes
`run_transform()` fans a batch across and merged by `TransformUsage.merge()`,
which is addition, so a 4-process worker reports about four seconds of them per
second of its own clock. Its `transform` phase measured 12.9 core-hours of wall
time against 50.1 of pooled CPU (decode included), a ratio of **3.88** — the
run's whole overshoot, and the reason `transform_parallelism` is now measured
and divided out. `decode_call` and `decoded_byte` are fitted from `length_hist`,
which is taken in the same processes, so they carry the same factor.

**The unwritten tiles.** The model charged 423 B on each of 715.8M
profile-tiles. The run wrote 167.4M of them and skipped **77%**: a profile
returns None wherever it has nothing to say, and `_run_counts()` stores
nothing for those. `bytes_per_output_tile` was fitted over the written tiles
alone (`output_bytes / (written - gap_tiles)`) and then charged on every tile
in a record, a denominator and a numerator that never matched. Predicted write
cost 4.8 core-hours against 0.5 measured. `written_share` is what reconciles
them, and it is per profile because the profiles disagree wildly: 0.411 for
`land` against 0.057 for `cropped_waterways` on that run.

Refitting that run with both corrections and predicting it back gives **16.8
core-hours against 15.9 measured, 1.06x**. The per-worker spread is what
actually improved: the worst over-prediction falls from 16.2x to 4.4x and the
90th percentile from 12.2x to 2.8x. `written_share` alone is worth little on
the total (3.79x to 3.58x) and a great deal on the shape — it is what stops the
model handing a worker 14.9M ocean tiles and predicting 762s for work that took
1.5s, because nothing was written at all.

### What the model still cannot see

Its R² against those durations caps out at **0.29**, and it under-predicts
the slow tail by 2-4x: the worst worker was predicted at 8m and ran 34m12s.
What is missing is content complexity. A dense coastline tile costs far
more to clip and union than an open-ocean tile of the same byte length, and
nothing in a manifest record exposes that -- the same effect the
over-chunking below exists to absorb. Treat the printed prediction as a
ranking signal, not an estimate -- for the profiles' share, which is where the
content complexity lives, "Measured tile blocks" replaces it with what the
same piece of the world actually cost last time.

Two further cautions on the fit. The run it is fitted against was itself
partitioned by the normalized model, so the predictors are correlated by
construction (bytes against output tiles at -0.80), which is why the
regression alone cannot pin the small coefficients and the direct
observations above carry them instead.

**`decoded_byte` used to carry an exponent, and no longer does.** A
`DENSITY_EXPONENT` of 1.5 charged decode on `length ** 1.5`, on the argument
that a bigger tile is also a denser one. The measurement never supported it:
sweeping the exponent from 1.0 to 2.0 moved the model's correlation only
between 0.499 and 0.521, a flat top around 1.4-1.5 rather than a peak, and
1.0 -- plain linear -- sat inside that band. A coefficient that cannot be
distinguished from 1 is not a measurement, and it cost two things: a sweep in
the calibration path, and a `length_hist` fit whose byte term had to be
reconstructed as `count * mean_length ** e` instead of being the bucket's
byte total outright.

So decode is linear in byte length now, and `decoded_byte` is rescaled from
the `2e-9` fitted at the old exponent by `sqrt(3000 B)`, roughly the
reference run's mean distinct-entry length, which leaves decode the ~quarter
of per-byte cost it held there. **Without that rescale the change would have
been wrong rather than simpler**: at `2e-9` per linear byte the decode term
collapses ~55x (sqrt(3000)) and `fetched_byte` swallows the model.

One structural consequence to keep in view: `fetched_byte` and `decoded_byte`
are now both linear in `length` and both charged once per distinct entry, so
they are exactly collinear and no regression against worker durations can
separate them. They stay two coefficients because they are measured from
different places -- `fetched_byte` from `fetch_seconds / fetched_bytes`,
`decoded_byte` from the `length_hist` fit -- not because a duration can tell
them apart.

What the exponent was reaching for is real and still unmodelled. `length` is
the *gzipped* length in the archive while the work scales with the
*uncompressed* payload, and denser tiles compress better; MVT's zigzag-varint
delta encoding spends fewer bytes per vertex the denser a tile is; and GEOS
clip and union are superlinear in vertex count. All three bend the true curve
upward. None of them is a power of the compressed length, which is why a
fitted exponent picked up so little of them.

### Measuring a run

Every coefficient above is a guess until something measures it, so a worker
reports what it actually did. `usage.py` prints one `usage:` line per scope,
in `name=value` form: a whole run's budget is one `grep '^usage:'` over the
job logs, and that grep is what the next run's coefficients are fitted from.

Two scopes in the log, and a third in the usage file alone:

- **`scope=profile`**, one per profile per worker: `written`, `skipped`,
  `blobs`, `transform_seconds`, `output_bytes`, `gap_tiles`, `gap_skipped`,
  `gap_bytes`, and the finished shard's size on disk. The gap counts come in
  both halves because `written_share` is a real-tile figure: netting only the
  written gaps out of `written` while leaving the skipped ones in `skipped`
  would read a profile that declines to fill gaps as one that declines tiles. `blobs` counts *distinct* blob objects
  rather than rows, so `written / blobs` is the storage amplification -- the
  number that decides whether the deduplicated shard layout pays for itself
  (see "Shard layout"). The gap figures are reported apart from the real
  tiles' so a fit can keep two populations of very different size from
  averaging each other out. No per-profile coefficient comes from this row
  any more -- a profile's cost is kept per tile block -- but its seconds and
  bytes are what `transform_parallelism` and `written_byte` are fitted
  against.
- **`scope=worker`**, one per worker: the archive key it read, wall-clock
  seconds split by phase (`fetch`, `transform`, `write`, `close`), bytes
  fetched, entry counts, and the decode totals including `length_hist`.
  `PhaseSeconds` nests exclusively, so the `write` time spent inside the
  transform loop is not also counted as `transform`.
- **`scope=blocks`**, one per worker, written to `--usage-out` and never
  printed: the profiles' seconds and written bytes summed per home tile
  block, as `seconds=block:seconds|...` and `written_bytes=block:bytes|...`.
  It is hundreds of blocks per worker, which is a file's
  business and not a log's; see "Measured tile blocks".

**There used to be a third, `scope=chunk`, one line per transform chunk, and
it was removed.** The reasoning for it recorded here was that
`TRANSFORM_CHUNKS_PER_WORKER = 8` turns 128 worker measurements into ~4,096
chunk ones, and that chunks vary in composition where the deliberately
equal-cost worker blocks do not, which would break a collinearity a
worker-level regression cannot get past. That argument describes a fit nobody
wrote. Every consumer of those rows in `calibration.py` summed them first --
`sum(output_tiles)`, and `length_buckets()` adding the histograms together --
and no regression ever ran chunk against chunk. The variance the per-entry fit
actually draws on comes from the 32 log2 length buckets, not from the number
of rows carrying them, and summation is associative: one aggregated row per
worker yields bit-identical coefficients. What the per-chunk lines bought was
~32 lines of log per worker.

The worker is therefore the reporting unit. Each chunk still measures itself in
the process that ran it -- that is where the clock is -- and `TransformUsage`
travels back through the pool and is merged into the worker's own totals
(`TransformUsage.merge()`), which is addition throughout. The archive key
travels on the row with the measurement, so a fit never has to guess which
archive a number came from, and two runs' logs concatenated by accident are
detected rather than averaged together.

`decode_seconds` and `transform_seconds` are reported separately because they
are different work on different inputs. `Tile.decode()` is gunzip plus
protobuf, close to linear in byte length, paid once per *distinct* entry;
`profile.transform_tile()` is shapely clip and union, paid once per profile
and driven by content complexity rather than byte length. Fused into one
number they are indistinguishable, and an R² of 0.29 cannot be read as either
"the model is bad" or "decode is clean and all the variance sits in
transform". The split also makes profile count a real factor: a run costs
`1 x decode + N x transform`, which one fused measurement cannot express.

`length_hist` accumulates the decode into log2 buckets of entry length
(bucket `i` holds lengths whose bit length is `i`, i.e. `[2**(i-1), 2**i)`),
as `bits:count:bytes:decode_seconds`. The profiles' seconds are not in here:
they are on the `scope=profile` rows, one per profile.
Buckets rather than 43M individual samples: the curve is what the per-entry
coefficients are fitted from, and it fits in a log line. It is also what
retired the decode exponent (see "Where the coefficients come from") and what
would catch decode going superlinear again.

The instrumentation is `2 + N` `perf_counter()` calls per distinct entry, for
`N` profiles: one clock serves as each profile's end and the next one's start,
so attributing the seconds per profile costs one extra call per profile rather
than two. At 26ns a call that is 3.4s across the reference run's 43,272,366
distinct entries at two profiles -- 0.006% of its 15.7 core-hours.

Peak RSS is not measured. It used to be read twice -- a `RUSAGE_SELF` peak
per chunk alongside the worker's `RUSAGE_CHILDREN` figure, because the latter
is the *maximum* over finished children rather than their sum and alone
under-reports a pooled worker by up to `--transform-workers`x -- and both
were removed. Anything added back needs that pair, not one half of it, and
needs to scale `ru_maxrss` by platform: `getrusage` reports it in bytes on
macOS and in kibibytes on Linux. Every `usage:` byte figure is bytes.

`WORKER_SETUP_SECONDS` sits next to `AXIS_SECONDS` and is a property of the
runner rather than of the run: runner boot, artifact download, and the
`pip install` of the dependencies. It does not change partitioning -- a
constant every worker pays alike cancels out of every ratio -- but it changes
two things that matter. The printed core-hours stop hiding it inside the byte
terms (128 x 39s is 1.4 of the reference run's 15.7 core-hours, about 9%),
and the `worker_count` search has to weigh it, every extra worker bringing
its own setup with it.

### The state branch, and the job that writes it

The whole loop, in the order it runs:

1. **`prepare-shards`** checks the calling repository's `state` branch out
   (`continue-on-error`, because before the first run there is no such branch)
   and passes `state/axes.json` as `--axis-state`. A missing file is not an
   error; it means nothing has been measured yet. For the archive this run
   reads it takes the median of each recorded coefficient, falling back to the
   reviewed one only where nothing was measured. That is what the run is then
   partitioned and sized by. Nothing in that file is about a profile: where
   `state/blocks/` exists it also passes that as `--block-state`, for the
   profiles' measured seconds and written bytes per tile block, and a block
   nothing measured is charged what the profiles declared.
2. **Each worker** writes its two-or-more `usage:` lines to `--usage-out` as
   well as to its log, and uploads that file. A file, not a log scrape: the job
   that fits these has artifacts, not log access.
3. **`merge-axes`** is the single writer, the way tiledistillery's
   `record-timings` is. It collects every worker's file, fits the run, appends
   one observation per coefficient to its own ring buffer, and pushes -- and
   the same for every tile block, into its archive and profile set's own file. It
   refuses to push unless `--expect-workers` matches what reported, because a
   partial run is a biased sample -- the workers that failed are the expensive
   ones. A complete run is always pushed: a new measurement describes the
   code and the runners as they are now, and the median over the history is
   what keeps one odd run from moving the model.

`merge-axes` pushes with the caller's own `GITHUB_TOKEN`, so the calling job
has to grant `contents: write`; the default for a repository is `read`. It
also needs `actions: read` to time the run's worker jobs, which is where
`worker_setup_seconds` comes from: a job's span less the `wall_seconds` its
process reported, the median across workers. Without it the push still
happens and that one coefficient stays at the reviewed 39s. The job
deliberately does not declare that permission for itself, because a called
workflow may only narrow what its caller granted and never widen it, and that
ceiling is checked before any `if:` is evaluated -- a `permissions:` block here
would refuse the whole run at startup for every caller that had not granted
write, including one passing `axis_state: false` precisely because it never
wanted the branch. Inheriting keeps the failure where it belongs: withhold the
permission and the push fails inside a `continue-on-error` job, costing the
calibration and nothing else.

The write is a read-modify-write against the blob sha through the contents
API, so two pipelines finishing together cannot lose each other's
observations: the second write is refused and the whole change reapplied to the
newer document (`state_branch.update_json`). The branch is an orphan sharing no
history with the default branch, so a state write can never touch what the run
was built from.

`tilealchemist-calibrate` remains the by-hand tool. It prints the same
proposal and writes a flat `calibration.json` for `--axis-seconds`, and it
touches no branch. The automatic path is `merge-axes`; the manual one is
`calibrate`, and neither edits `cost.py`.

### Why no budget caps a worker

The per-record coefficient used to carry a memory bound. At its measured time
cost (~1e-6) nothing holds deduped repeats together, and replaying the
reference run put 8.5M records on one worker -- `read_manifest()` materializes
those as namedtuples, about 1 GB before a single tile is fetched. Raising the
coefficient to 9e-5 pulled that to 3.8M:

| per record | max entries in a block | manifest RAM | correlation |
| --- | --- | --- | --- |
| 1e-6 (measured) | 8.5M | 0.95 GB | 0.538 |
| 9e-5 | 3.8M | 0.43 GB | 0.537 |
| 2.5e-4 | 2.0M | 0.22 GB | 0.512 |

That worked, but it could never *guarantee* anything, and it is worth being
precise about why. The table is a measured curve with no closed form: 9e-5
produced 3.8M on **that** run, and on a run with a different record
distribution the same coefficient produces something else. A price cannot
enforce a limit; it can only make crossing it expensive.

What followed was a run of steadily more elaborate caps, each one a hard
condition in `partition_by_cost()`'s loop beside the `_share_end_weight`
comparison: **records per block** at a measured ~184 B per `Entry`, **peak
batch bytes**, a single `--worker-disk-budget` that the manifest, the one
spooled batch and the shards were all charged against, and finally
**`--max-tiles`**, the output tiles one worker may write. Each was more
faithful than the last, and the byte ones each needed the same thing to
work: a model of what a source byte turns into on the runner's disk, which
in turn needed numbers only a profile could give -- a declared
`storage_reduction`, a weighed `gap_tile_bytes()`, a measured per-row
overhead. The accuracy of a cap meant to stop a worker filling a disk rested
on a chain of ratios, one of them declared by hand in somebody else's
repository. `--max-tiles` needed none of that, a tile count being the unit
the pipeline is already denominated in, but it still needed an operator to
convert a disk size into a row count and pass it in.

**None of them are here now, and nothing caps a worker.** `--max-tiles` was
unset by default, because nothing about a *correct* run needs it, and the
runner disk it existed to protect has not been the binding limit on any
measured run. A cap nobody sets is a partition rule, a reservation pass, a
CLI flag, a pipeline input and two warning paths carrying no weight, and all
of it is gone. What the caps taught is kept here rather than in code: a
price cannot enforce a limit, a cap needs a unit the pipeline already
counts, and neither is worth carrying before a run has actually hit the
wall. When one does, what goes in is a check against that wall -- the
specific resource, measured -- rather than another general budget.

`manifest_record` stays at its measured ~1e-6 either way, which leaves the
cost model with no coefficient in it that is not a measurement.

**What a block writes is still counted, in rows rather than records.**
`count_output_tiles()` sums `run_length` over the block and `prepare-shards` logs
it, so:

- a **deduped run** contributes every tile id it covers. The archive stores
  one blob for 40 identical ocean tiles and the worker writes 40 rows, because
  a shard is flat and only the merge deduplicates (see "Shard layout").
- a **gap** contributes every tile id it covers, for exactly the same reason.
  It is *priced* differently, though: a gap record is charged its write and
  nothing else. It has no source bytes to fetch or decode, and
  `transform_gap()` answers the whole run with one call, so charging a gap a
  decode or a profile's per-tile seconds bills it for work no worker does. At
  `GAP_CHUNK_SIZE = 200_000` tiles per record that used to be a rounding error;
  it is wrong either way, and the fix is one branch in `_record_costs()`.
- a **record** contributes nothing by itself. Its ~184 B of namedtuple
  against a row's ~250 B of shard is not worth a second axis.

It is counted **per worker, not per profile**: a run building two profiles
writes two shards of that many rows.

    worst block: 458123 records writing 7798155 output tiles (7340032 of
    them gap tiles)

That line is a measurement now rather than a verdict, and it is worth
reading as one. The gaps are spread across workers before the real entries
are (see "Parallelism"), so a worker's rows are the sum of two
independently balanced blocks. While `--max-tiles` existed the real
partition was handed each worker's gap rows as a reservation to start owing,
so that the two halves would add up inside one cap; measured on a 6K-entry
synthetic manifest whose 120 gaps came to 600K of its 653K output tiles, at
16 workers under a 40K cap, that held blocks 0-14 between 39,976 and 40,040
rows where an unreserved partition put eight of them 8% over. With no cap
there is nothing for the halves to add up inside, both are balanced on cost
alone, and how lopsided the sum gets is what this line reports.

Counting records, the unit used before any of this, fails the other way: it
is *exactly* even and says nothing about cost. In a 128-worker planet run it
gave every worker 458K entries and between 32MB and 3,494MB of tile data,
and those workers ran between 26s and 57m42s. Across all 128, job duration
correlated with entry count at 0.04 and with byte volume at 0.82 -- which is
the finding the whole model rests on, and the one number here that has held
up across every run since.

Since only ~20 jobs run concurrently anyway, balancing does not move the
floor: 16 core-hours over 20 lanes is ~48 minutes either way. What it
removes is the tail. That run spent its last 19 minutes at a concurrency of
**one**, waiting on a single worker.

### A gap is a tile like any other

A gap record is a `tile_id` range the archive holds nothing for, and it is
cheap in exactly one way: there is nothing to fetch. It costs no download
and, carrying `length=0`, it can never widen a batch -- both
`_split_on_wide_holes()` and `peak_batch_bytes()` skip it. Everything else
about it is an ordinary tile. It occupies a manifest record like any other,
and the worker writes one output tile per tile id it covers, which for one
unbroken ice sheet interior is hundreds of thousands of rows.

That is why a gap is chunked at all: `GAP_CHUNK_SIZE` cuts every gap into
200K-tile records so that one unbroken gap cannot land whole on a single
worker. `Profile.transform_gap()` is called once per run and its bytes reused
for every tile in it, so the profile work is free -- but the rows are not,
and a worker still writes every one of them.

### Why more chunks than processes

Cost-balanced chunks still are not equal-cost chunks: the weight is an
estimate, and tile content varies inside a chunk. A real run's four chunks
of 269648/269648/269648/269645 entries finished 2m17s, then a further
9m15s, then a further 17m9s apart (dense coastline vs. open ocean). That is
why chunk count exceeds process count: it turns `ProcessPoolExecutor`'s own
call queue into a work queue, where a process that finishes early pulls the
next pending chunk instead of idling while one unlucky core grinds through
a dense coastline. Balancing gets the chunks roughly even; over-chunking
absorbs whatever imbalance is left. `TRANSFORM_CHUNKS_PER_WORKER` is 8:
enough to keep the idle tail at roughly an eighth of a process's share,
while leaving per-chunk overhead (one profile reimport, one blob-slice
pickle) noise against multi-minute chunks.

Both together are what close the tail. The same planet run's worst worker
split 452687 entries into 33 entry-count chunks of 0MB to 614MB, and spent
its final 11 minutes running that one 614MB chunk on one of four cores.
Weighted, the same manifest yields 32 chunks of 5MB to 107MB.

Each task reloads its profiles from their own `--profile` paths rather than
receiving live instances: profiles loaded via `load_profile()`'s
`importlib.util.spec_from_file_location()` aren't registered in
`sys.modules`, so the default pickler used to hand work to a pool worker
can't reconstruct them there. `.github/workflows/test.yml` runs both of
these paths against real OpenFreeMap data on every push/PR, as a normal
low-zoom run rather than as a separate test, and runs a second one against
a real Protomaps build alongside it (see that file for why the two calls
aren't symmetric).

Each chunk's output is written to its profile's mbtiles as soon as that
chunk comes back, then dropped: `run_transform()` yields each chunk's
results as it completes and `shard_worker.py` writes that chunk out
immediately, rather than collecting every chunk's output first. A worker
with millions of real entries would otherwise hold its entire shard's
transformed output in memory for the whole transform phase, which is
exactly what ran a worker out of memory in production. This way peak memory
is bounded by how many chunks are in flight at once, not by the shard's
total size.

Yielding per chunk only bounds it if nothing else is still holding the
chunks already yielded, and on the pooled path the `Future` each chunk came
back on is such a holder: a `Future` keeps its result for as long as the
`Future` is alive. `_pooled_chunk_results()` therefore drops each one from
its `pending` map as it yields that chunk, rather than keeping the whole map
until the pool shuts down. Keeping the whole map pinned every chunk's
output for the whole phase, putting the bound straight back where it was
before chunking — a planet run's two largest shards died to exactly that,
on runners that report it as `The runner has received a shutdown signal`.

Submitting is throttled for the mirror image of the same reason.
`_blob_slice_for_chunk()` *copies*, and `ProcessPoolExecutor`'s work queue
holds every argument until that chunk is dispatched, so submitting all of
them up front left the parent holding ~28 slices that roughly tile the
batch — the batch a second time, on top of the blob it already has.
`_pooled_chunk_results()` therefore keeps only `max_workers + SUBMIT_LEAD`
chunks in flight and submits the next one as it takes a result back. The
lead is what stops a process idling while the parent writes, and measured on
a 32-chunk batch across 4 processes the parent holds 7 slices where it used
to hold 32. The same throttle bounds the other direction too: completed
results can no longer pile up when the serial sqlite writer falls behind
four parallel transformers, which is exactly the combination an ocean-heavy
worker produces.

Where several chunks are already done, the lowest-numbered one is taken
first. That is a tie-break among *ready* futures, never a wait for a
particular chunk, so no chunk blocks the head of the line.

A chunk is also committed as it is written, rather than the whole shard
running from `init_mbtiles()` to `close_shards()` inside one open
transaction. With `synchronous = OFF` a commit costs nothing, and it caps
the journal instead of letting it grow to the size of the shard.

That makes a half-written shard *more* plausible rather than less, so the
shard says when it is whole: `close_shards()` writes
`tilealchemist_complete` into `metadata` in the same commit as the last
tiles. A worker killed mid-write leaves a database without it, which is what
lets a merge tell a truncated shard from a small one -- `upload-artifact`
runs with `if: always()` and `if-no-files-found: ignore`, so today such a
shard is handed on silently and merges into a quietly short layer. (The
merge-side check itself is not in this repository yet.) `tile-join` ignores
the unknown metadata key; verified locally, output byte-identical with and
without it.

Free disk space is not checked: a worker that fills the disk finds out as an
`ENOSPC` in the middle of an `INSERT`. A pre-batch check that warned below a
threshold lived here and was removed; add it back if the failure mode turns
out to be worth the warning.

### Runs stay runs until the writer

`transform_batch_blob_multi()` used to expand every `run_length` into one
tuple per output tile, and `write_output_tiles()` turned each of those into
its own `INSERT`. Measured, the cost of that expansion splits three ways and
only one of them is memory:

| level | per output tile | why |
| --- | --- | --- |
| live objects in RAM | 156 B | the 4-tuple plus its three ints |
| pickle across the process boundary | 13 B | pickle memoizes the shared `bytes` |
| **disk** | **the whole blob** | a flat `tiles` table, deduplicated only at merge |

The blob is therefore not multiplied in RAM; it is multiplied on disk, which
is what "Shard layout" is about. What the expansion does cost in RAM is the
tuples: the reference run's 357,913,942 output tiles per profile against its
58,679,705 records is 6.1x, and its tile-heaviest worker held ~2.41 GB of
them per profile where runs hold ~0.4 GB. Across the process boundary the
saving is larger still, pickle having already memoized the shared blob: one
pure run of 50,000 tiles goes from 550,397 bytes to 236.

Keeping a run a run until the writer also makes an all-ocean entry O(1)
rather than O(run_length): a run whose output is `None` no longer builds the
millions of tuples it would throw away on the next line. And it lets
`write_output_tiles()` use `executemany` over a generator -- the shape
`write_gap_tiles()` already had, which is why that function is now one call
into the common path plus its log line, and why a gap entry needs no special
writer at all: a gap *is* a run, one shared blob over `run_length` tiles.

Folding *adjacent* entries that share an `(offset, length)` into one run as
well would take the same ratio to 8.3x. That is a second and independent
change, deliberately not made here.

### Shard layout

`--shard-layout` picks how a worker's mbtiles stores its tiles. `flat` is one
`tiles` row per tile, the layout every shard has had until now. `dedup` is
the layout `tile-join` writes itself:

    map    (zoom_level, tile_column, tile_row, tile_id)   UNIQUE (z, x, y)
    images (tile_id INTEGER PRIMARY KEY, tile_data)
    tiles  a view joining the two

The default is `flat`, and the switch is staged on purpose: `dedup` pays only
where a shard really repeats blobs, which is what `usage:`'s
`written / blobs` measures, per profile, `land` and `cropped_waterways` being
free to disagree. Per written tile, with `B` the blob size and
`A = written / blobs`:

| layout | per written tile |
| --- | --- |
| flat | `B + ~26`, the row inline plus its index entry |
| dedup | `~35 + B/A` |

| B | A | flat | dedup | factor |
| --- | --- | --- | --- | --- |
| 200 | 6.1 | 226 | 68 | 3.3x |
| 200 | 2 | 226 | 135 | 1.7x |
| 1000 | 6.1 | 1026 | 199 | 5.2x |
| 200 | **1** | 226 | 235 | **0.96x, a loss** |

Break-even is `B * (1 - 1/A) > 9`: from `A >= 2` the layout wins at any
realistic blob size, and at `A = 1` it loses about 4%. The risk is therefore
"needless complexity", not "catastrophe" -- but `A` has to be measured on a
real run before the default moves, and `flat` stays a release as the way back.

**The key is a synthetic counter, not a content hash.** A 32-character hex
digest costs ~33 B in *every* `map` row and again in every `map_index` entry;
across the reference run's 357.9M output tiles that is ~23 GB of pure key
material, which would eat the whole saving. A small integer costs 1-4 B.
`INTEGER PRIMARY KEY` is a rowid alias besides, so `images` writes become a
purely sequential append with no B-tree of their own and the merge's join a
direct rowid seek, where a TEXT hash would need its own index and a random
B-tree insert per distinct blob.

Blobs are matched by identity against the *previous* one -- `is`, against a
variable holding a strong reference for the whole loop, so a freed address
cannot be reused under it -- rather than by hashing. `id()` would not do:
a freed address gets reused, and an `id()`-keyed dict can confuse two
different blobs. That adjacency window covers exactly the two cases that are
adjacent by construction: a run (one object over N tiles), and two
consecutive entries sharing an `(offset, length)`, which already share one
`outputs` list. Duplicates further apart get a second `images` row, which is
fine: PMTiles' content hash catches them in the merge exactly as it does
today. `write_gap_tiles()` collapses to a single `images` row per shard for
free, every gap entry carrying the same `gap_data` object -- measured on a
real run with hand-made gaps, 111 gap tiles became one `images` row and one
`map` row per tile.

The `UNIQUE` index stays, moving from `tiles` onto `map`, for two independent
reasons. An `IntegrityError` on a repeated `(z, x, y)` is a wanted invariant
-- it means a partitioning bug, and crashing is the right answer, which is
also why neither `INSERT OR REPLACE` nor `OR IGNORE` belongs on `map`. And it
carries the *merge*: `tile-join` reads
`... from tiles order by zoom_level, tile_column, tile_row`, and without the
index sqlite would have to sort those rows -- blobs included -- externally.

`tile-join` supports this layout by construction rather than by accident: it
writes this schema itself and reads only through `tiles`, so it can read its
own output back. Both halves verified locally: a flat and a deduplicated
shard of the same tiles produce byte-identical PMTiles content and identical
headers, a mixed invocation (one of each, which a half-migrated
`build-shards` can hand it) works, and `EXPLAIN QUERY PLAN` on tile-join's
own query gives `SCAN map USING INDEX map_index` plus a rowid seek into
`images`, with **no** `USE TEMP B-TREE FOR ORDER BY` -- at 490K map rows and
after `ANALYZE` too.

### Learning the coefficients from the last run

`AXIS_SECONDS` in `cost.py` is the reviewed fallback, and the first run of
anything uses it. After that, `tilealchemist-calibrate` reads the `usage:`
lines a finished run printed and proposes what the next one should use.

Each coefficient comes from the measurement that isolates it:

| coefficient | fitted from |
| --- | --- |
| `fetched_byte` | `sum(fetch_seconds) / sum(fetched_bytes)` over workers |
| `written_byte` | `sum(write_seconds) / sum(output_bytes + gap_bytes)` over the profile rows |
| `decode_call`, `decoded_byte` | two-parameter least squares over the `length_hist` buckets, against `(count, bytes)` |
| `manifest_record` | the slope of `setup_seconds` against a worker's record count |
| `transform_parallelism` | `sum(decode_seconds) + sum(transform_seconds)` over the profile rows, `/ sum(transform_seconds)` over the workers |

No coefficient is per profile. A profile's own cost -- its shapely and the
bytes it writes -- depends on which archive it reads and which profiles run
beside it, so it is measured per tile block, under that archive and profile
set (see "Measured tile blocks"). A profile's declared `seconds_per_tile`,
`bytes_per_output_tile` and `written_share` only price the blocks nothing has
measured yet.

The write cost is charged on bytes rather than on tiles. A tile is only as
expensive to store as it is large, and how large it is belongs to the profile
that shaped it: a coastline profile's tiles are not a label profile's. So the
old flat `output_tile` second is now `written_byte` times the bytes written,
the first a property of the runner's disk and the second of the profiles --
measured per tile block, or declared where no block was measured. At
the reviewed figures the product is the same 4.9e-7s the single coefficient
carried, so splitting it changed no prediction on the day it landed -- it only
gave the two halves somewhere separate to move.

**A gap tile's size is measured, not fitted.** `transform_gap()` answers every
gap tile in a run with the same bytes, so `prepare-shards` asks each profile
once at plan time and gets an exact figure. That is why `gap_bytes()` is a
*method* on `Profile` rather than a number: the default implementation just
measures its own `transform_gap()`, and a profile that can answer without
building the tile overrides it. There is no
statistic to estimate and nothing for the state branch to remember. That also
keeps gap tiles out of the measured block bytes, which is the whole reason to
do it: a run is anywhere from 0% to 94% gap tiles, and the two populations differ
by an order of magnitude, so one averaged figure would be set by the mix rather
than by either. On a `land`-style profile over open ocean the measured gap tile
is 54 B against the 250 B default a real tile is assumed to weigh.

**Nothing settled here is written back onto a profile.** `prepare-shards`
resolves the two sources of a profile's cost that are not per block --
measured gap bytes and the profile's own declared estimates -- into one
`cost.ProfileCost` per profile and holds it
(`shard_prep._declared_profile_costs()`).
A `Profile` stays the behaviour it is; it declares estimates and answers
questions, and it never doubles as the mutable ledger of what a run decided to
charge. `cost_weights()` takes those settled costs as an argument, so what a
prediction was made from is visible at the call rather than sitting on an
object somebody may have rewritten.

A block's written bytes are therefore measured from the payload the
transform actually produced, counted once per output tile and summed across
the profiles as the run goes, and so is the `output_bytes` that `written_byte`
is fitted against -- rather than from the finished shard's size on disk. `shard_bytes` stays reported, but as the storage-amplification
diagnostic it always was, not as the basis of a coefficient: it carries sqlite
page overhead and indices, and under `--shard-layout dedup` it carries the
collapse of a whole gap into one `images` row.

Every aggregate is `sum(seconds) / sum(units)`, never the mean of per-unit
rates. That is the same choice tiledistillery's `fit_seconds_per_byte()`
documents: small units carry a fixed overhead, so averaging their rates lets
the smallest ones dominate. Here that would let the worker holding one
distinct entry weigh as much as the one holding 2M.

The entry-cost fit targets the decode alone. It used to target
`decode_seconds + transform_seconds` together, because the model charged both
to the same per-distinct-entry axes -- but the profiles' own seconds are now
measured per profile and charged to the profile that spent them, so folding
them back into the archive's coefficients would bill one profile's shapely to
the other's. The byte term is the bucket's byte total outright, decode being
linear in length; the sweep that used to search for an exponent there is gone
with it.

This borrows tiledistillery's mechanism. `leaves.py` looks each Geofabrik
region up in `timings.json`, where a measurement *beats* the model; the fitted
`seconds_per_byte` exists only to place regions that were never built. A
worker block is recomputed every run, so `worker-042.bin` means something
different next time and is no key to hang a measurement on -- but a fixed
range of tile ids is, and "Measured tile blocks" is the per-region half of
tiledistillery's scheme built on that. The coefficients here are the other
half, the fallback, and they have stable keys too: the archive and the
profile, which is what the state branch files them under. The ring buffer, the median, the
single-writer job and the orphan `state` branch are all the same shapes
tiledistillery uses, down to `HISTORY_LENGTH = 5`.

Two guards, because a bad calibration does its damage quietly, inside
`partition_by_cost()` on every later run:

- **Every worker must have reported.** A partial run is a biased sample --
  the workers that failed are exactly the expensive ones.
- **A coefficient is the median of the last few runs, not the newest one.**
  `axis_state.py` keeps `HISTORY_LENGTH = 5` observations per coefficient and
  reads the median of them. A mean would let one bad runner through at a fifth
  of its full weight; a median ignores it outright, which is the whole reason
  the history exists. The model shapes the distribution it is then fitted on,
  and a median over several runs is what keeps that feedback slow enough to
  stay stable.
- **The push is all-or-nothing, and it is not the build's problem.** The
  `merge-axes` job runs `if: always()` so a failed build leg does not cost the
  others' measurements, and `continue-on-error: true` because a calibration is
  an optimisation and never a reason to fail a finished build.

`worker_setup_seconds` is deliberately not learned from logs alone. A
worker's own `setup_seconds` is the *in-process* residual (wall clock minus
the phases); runner boot, artifact download and `pip install` happen before
the process exists and no line in its log can see them. The proposal
therefore leaves it alone unless `--runner-overhead-seconds` supplies that
half, and says so.

The coefficients are not all of a kind, and the state splits them by what
each one actually depends on:

| group | key | coefficients |
| --- | --- | --- |
| source | host + schema | `fetched_byte`, `decode_call`, `decoded_byte` |
| shared | none | `manifest_record`, `written_byte`, `worker_setup_seconds`, `transform_parallelism` |
| block (`state/blocks/`) | host + schema, profile set, tile block | the profiles' pooled seconds, the bytes they wrote |

The archive's fetch rate and tile density belong to the provider. What is
left over is tilealchemist's own bookkeeping, the runner's disk and the
runner's cores, which belong to neither — `transform_parallelism` is shared
for exactly that reason: how many seconds of shapely fit into one second of
wall clock is a fact about the machine, not about the profile that spent them,
which is why a block's figure stays the CPU seconds it was measured as.

**A profile has no group of its own here.** It used to: `seconds_per_tile`,
`bytes_per_output_tile` and `written_share` per profile name. But a profile's
cost is not a property of the profile alone -- profiles share derived work, so
one profile's seconds only mean anything beside the same others, and the same
profile clips different geometry out of a different archive. Keyed by name
alone, a figure measured on Protomaps would have priced the same profile on
OpenMapTiles. The tile blocks already carry exactly the right key, so they
carry the profiles' whole cost, and `record_run()` drops a `profiles` section
it still finds in an older `axes.json`.

**The archive's build is deliberately not part of its key.** A provider's
fetch rate and tile density are properties of the provider, not of this
month's extract, and putting the build in the key would start every build's
history from nothing -- which is the one thing a median over several runs
cannot survive. The build is recorded beside the coefficients so a reader can
see which extract last moved them, and that is all it is for.

A single constant in the library cannot be right for every caller at once,
which is why the state lives in the *calling* repository, on its own branch,
the way tiledistillery keeps `timings.json` -- and not here.

What makes the fit identifiable in the long run is runs of *different shape*
-- planet, a regional extract, a high-zoom range. That mixture is exactly
what the earlier normalized model could not survive ("a z0..z11 ocean-heavy
range is almost all sqlite inserts, a high-zoom extract is almost all
decode"), which makes it the training set that separates the coefficients
rather than the one that confounds them.

### Measured tile blocks

The coefficients above price a record by what its manifest entry says, and a
manifest entry cannot say how hard a tile is to clip: R² 0.29, and the slow
tail under-predicted 2-4x (see "What the model still cannot see"). What *can*
say it is the last run that built the same tiles. So the profiles' seconds,
and the bytes they wrote, are measured per **tile block** and kept, and the
next run charges a block what it cost last time instead of what the profiles
declared per tile.

**A block is a fixed range of tile ids, not a piece of a partition.**
`tile_blocks.tile_block()` cuts every zoom level into runs of
`2 ** TILE_BLOCK_BITS` = 4096 consecutive Hilbert indices, a quadtree cell:
one z8 cell at z14, one z7 cell at z13, and the whole level from z6 down. Its
key is its first tile id. That depends on nothing but the tile id, so a block
is the same piece of the world in every run -- whichever archive build, zoom
range or worker count the run has -- which is exactly the stable key a worker
block lacks. A z0..z14 run has about 87K of them.

**Only the profiles' share is measured; fetch and decode stay modelled.** The
two halves differ in kind. Fetch and decode are linear in the bytes, and this
run's manifest knows its bytes exactly, for this build; last month's
measurement of them would be strictly worse. Fetch also happens per range
request, not per block, and has no clean per-block figure at all. The profiles'
shapely, and how much of it they write, is the part no manifest predicts, so
that is the part a block carries: its pooled seconds and its written bytes,
the latter charged at the shared `written_byte`.

**A record belongs to its blob's home block, not its own.** A deduplicated
tile points back at the blob its first copy wrote, which in an archive written
in tile id order sits wherever that first copy's tile falls -- for an ocean
tile, near the very start. Keyed by its own block, each such record dragged
that far-away offset into whichever worker held its tile: on standardprofiles
run 37037515494 worker 0 fetched its 2.06M entries in one range request and
worker 2 its 2.54M in 84, all but one of them stretches of blobs that earlier
workers' tiles had written, read through up to 8 MB holes. So
`tile_blocks.home_blocks()` keys every record by the block of the record that
decodes its blob -- the first of its `(offset, length)` run, which
`collect_entries()` sorts lowest tile id first. The measurement, the charge
and the partition all use that key, so a block's records are the blobs it
decodes and every tile pointing at them.

**The key is the archive and the whole profile set**, as
`state/blocks/<source key>/<profile set>.json`, with the set as
`profile_combo_key()` gives it (`cropped-waterways+land`). Not the profile
alone: profiles share derived work through `Tile.derived()` -- the water
union, for one -- and whichever runs first is billed for it, so one profile's
seconds only mean anything beside the same others. Not the profile alone
across archives either: the same profile clips different geometry out of
OpenMapTiles than out of Protomaps tiles. A new combination starts with
nothing measured, and the model carries it until it has run once.

**How a block is measured.** `transform.py` already clocks every profile per
distinct entry; `TransformUsage.add_block()` also adds the sum to the decoding
entry's block, and `add_block_bytes()` adds every entry's written payload --
a deduplicated one's included -- to that same block. A block that wrote
nothing records zero bytes, so "costs nothing" stays distinct from "never
measured". The seconds are pooled, summed across the worker's process pool,
and are divided by `transform_parallelism` when charged, the same as the
per-tile figure they replace. Each worker writes both as its `scope=blocks`
line (`seconds=` and `written_bytes=`), `merge-axes` sums them across workers
-- a block split between two workers, or a blob's run cut by a pool chunk,
still comes to one figure -- and appends one observation per block to that
block's own history, `HISTORY_LENGTH` deep, the median read back. It records
nothing unless every worker that reported also reported blocks, and no bytes
unless every one of them reported bytes: same biased-sample rule as the
coefficients.

**How a block is charged.** `cost.py` charges a measured block's seconds and
bytes once, on the first of its decoding records it meets, and drops the
profiles' declared seconds and write from every record homed in it; a block
with no measurement keeps the declared per-tile charge. "First it
meets" and not "first in a run" because records travel in offset order,
which is what a worker fetches by, and in offset order one block's records
are interleaved with others': a deduplicated tile points back at the blob its
first copy wrote, however far away that is.

**How a run is split.** `partition_by_tile_block()` hands out whole home
blocks in byte offset order -- each where its first blob sits -- and fills
each worker until the next block would overrun its share, then starts the
next worker. So each worker is one stretch of the archive and one range
request, whatever order the archive wrote its tiles in. Tile id order, which
it used before, only came to the same thing for an archive clustered by tile
id, and handed a worker of any other a scatter of ranges. The shares end at fixed points of the
run's cumulative cost, `total * (i + 1) / N`, rather than at a per-worker
budget, so what one worker leaves short is the next one's to take instead of
piling up on the last. A block is never split, which keeps "what this block
cost" a figure one worker measured whole. Each worker's records are then
routed back out in offset order, so its manifest is still offset-sorted for
the fetch batching. `tile_block_groups()` does the grouping once per run --
one group index per record, in an `array('I')` -- and every worker count the
sizing tries reuses it. Gaps are still split by record, having no profile
seconds to measure.

The price of an indivisible block is that no worker's share can go below the
costliest one, and `prepare-shards` prints that figure beside how many of the
run's blocks were measured. Measured on a z0..z6 OpenFreeMap run of `land` +
`cropped_waterways` at two workers, the z6 block alone cost 99.7 pooled
seconds against 6.4 for z0..z5 together. The reviewed model predicted the two
workers at 1.1m and 1.2m, nearly even; they ran 9.8s and 57.8s. Re-planned
from that one measurement it predicts 1.2m and 2.7m -- the right way round,
the remaining overshoot being the modelled fetch and decode and a
`transform_parallelism` that run had no state for. At planet scale a share is
minutes and a z14 block seconds, so the bound is the low zooms, each a single
block; a smaller `TILE_BLOCK_BITS` is the lever if they turn out to bind.

**The file is versioned by what its blocks are keyed by.** Version 1 kept
seconds alone, under each tile's own block; version 2 keeps `seconds` and
`written_bytes` under the home block. A version-1 file is not read -- its
figures describe different sets of records -- and the first run that writes it
starts it afresh.

**The file outgrows the contents API's 1 MB.** Five observations for ~87K
blocks is a few MB even written compact, which `write_json(compact=True)` does
for this file. Reading back past 1 MB the contents API answers only with the
`object` media type and no content, so `read_json()` asks for that and falls
back to the git blob API, which serves up to 100 MB. The write side has no
documented limit short of that, but has not been exercised at full size yet.

### Sizing a run

**`prepare-shards` always picks the worker count itself; there is no flag for
it.** The pipeline was already built for that by accident: `_pipeline.yml`
generates the worker matrix in `gen-workers` *after* the archive walk, in the
same job, so the manifests exist by then. `gen-workers` counts
`manifests/worker-*.bin`, which makes the manifests the only truth about how
many workers there are -- and with no input left to contradict them, the only
possible one.

Partitioning is one pass over the records, so the search needs no closed
form:

    for N = SC, 2SC, 4SC, ..., 256:     # C = --concurrency, in practice 20
                                         # S = --worker-scale, 3 by default
        blocks = partition_into_worker_blocks(entries, gaps, N)
        if every budget holds for the worst block: take N

**The step doubles rather than adding one wave at a time.** Stepping by `C`
made the cost of the search proportional to the answer: a run that needed 240
workers paid twelve full partition passes to find out, each one a complete
sweep over every record, and eleven of them thrown away. Doubling makes that
five passes at most from 20 to 256, and a run that fits its first try -- which
is every run small enough for the concurrency to hold it -- still pays exactly
one, the same as before. Every candidate stays a multiple of the concurrency,
because doubling a multiple of `C` is a multiple of `C`.

What it gives up is granularity: at the defaults the search can only land on
60, 120, 240 or 256, so a run that would have fitted 90 workers takes 120. That is the right
trade in this direction. Overshooting the worker count costs
`WORKER_SETUP_SECONDS` per extra worker (~39s of runner boot, artifact
download and pip install) against blocks that are correspondingly smaller,
while undershooting the *search* costs a full partition pass per step for a
number nobody sees. And the tail this whole model exists to remove gets
shorter with more workers, not longer.

One limit. **Time** from `cost_weights()` against the 6h job cap, and
nothing else. Tiles were a second until `--max-tiles` was removed, and RAM a
third until the batch body moved to disk -- that one was fitted from the peak
batch a worker fetched, and there is nothing left for it to bound (see "Why
no budget caps a worker"). Time is what `worker_count` was chosen against
before any of the storage budgets existed, and what it is chosen against
again. `breaches()` still returns a *list*, because the shape of the search
does not change when a second limit comes back.

The limit falls as N rises -- more workers, smaller blocks, smaller spans,
smaller batches -- and only setup overhead and wave count rise, so the first
N that fits is the best one and the search is a loop rather than an
optimization. Both retired axes are kept here because what they demonstrated
is the search's behaviour rather than their own. On the tile axis: against a
6K-entry synthetic manifest of 653K output tiles at `--concurrency 4`, the
chosen N rose 4 -> 8 -> 16 as a 200K -> 100K -> 50K cap tightened, and at
each step every block stayed inside it, the remainder one included -- the
search only takes an N whose *worst* block fits, so a remainder that overran
is what made it try the next N. That run's candidates are 4, 8, 16, ...
either way, so doubling changed nothing about it. On the byte axis: against a
1.5M-entry, 77 GiB synthetic manifest the chosen N fell 120 -> 60 -> 40 -> 20
as that budget rose 4 -> 8 -> 14 -> 28 GiB, and tightening the job budget from
6h to 2h at 14 GiB raised it back to 60. It was measured under the old
one-wave-at-a-time stepping, and 120 and 60 are not candidates any more; the
axis it exercised is gone too (see the RAM note above). It is kept because
what it demonstrates still holds -- the log names which limit bound the run at
each step -- not as a result to re-derive.

**Only multiples of the concurrency are considered.** At 20 lanes and
roughly equal-cost blocks a run goes in waves, and the candidates fall on
whole ones:

| workers | waves | last wave |
| --- | --- | --- |
| 60 | 3 | 20/20 |
| 120 | 6 | 20/20 |
| 240 | 12 | 20/20 |
| 256 (the ceiling) | 13 | 16/20 |

Only the ceiling leaves a lane idle, and it is GitHub's number rather than a
choice. For contrast, 128 -- the old default, and not a candidate now -- is
seven waves with 8/20 in the last, the same seven waves 140 buys. It is an
idealization, since GitHub starts greedily as a lane frees rather than in
strict waves, but it is measurable and it is the cheapest saving available.

**Even a run that fits one wave is split into `--worker-scale` of them.** The
runners are not one machine. GitHub's hosted `ubuntu-latest` pool spans
several CPU generations -- EPYC 7763, 9V74 and 9V45, Xeon Platinum 8573C and
Xeon 6973P-C have all been reported -- and pins the image, not the CPU. A
protomaps planet run at 20 workers, one wave, showed it plainly: decoding
the same blob sizes ran at 0.61-0.70 of the pooled rate on four runners and
up to 1.30 on the rest. Those four were the run's four fastest shards, at
0.48-0.67 of their predictions, while the slowest ran 1.30 over. The model
was right on average -- 41.3m actual against 42m predicted across all 20 --
and the run still ended at 57m, because one block per lane leaves nothing
for a fast lane to take over.

Which runner a worker lands on cannot be known when the manifests are
written, so no static partition can correct for it. What corrects for it is
having more workers than lanes: GitHub starts the next queued worker the
moment a lane frees, so a lane that drew a fast runner works through more of
them, and the run ends near the average plus at most about one worker's
share rather than on its slowest runner. Every queued worker draws a fresh
runner, which averages the speeds further. At the default of 3, a run that
fits 20 workers is cut into 60 of a third the size; the price is
`WORKER_SETUP_SECONDS` and roughly half a minute of artifact upload per extra
worker, a few percent of a planet run, and more shard files for `merge`. The
costliest tile block is the floor on a worker's share, so a scale past the
point where shares reach it buys nothing. Doubling more *concurrent* lanes
would not do the same: it shortens every lane alike and leaves the spread
between fast and slow runners exactly as wide, relatively.

The **256** ceiling is GitHub's own: `_pipeline.yml` expands the worker count
straight into the `build-shards` matrix, and GitHub refuses more than that
many cells. `candidate_worker_counts()` therefore ends on 256 whether or not the
doubling lands on it, and `gen-workers` checks the manifest count against it
before the matrix is built rather than after the archive walk has already run.

The time budget is charged at `TAIL_SAFETY_FACTOR` (2x), not at face value,
because the model still under-predicts its slowest workers. It was 4x when
the model under-predicted its slow tail by 2-4x -- the reference run's worst
worker was predicted at 8m and ran 34m12s. Against measured block costs the
20-worker protomaps run above came in at 0.48-1.30x of prediction per
worker, the spread being runner speed rather than content, so 2x covers the
slowest runner with room left. Sizing against a *hard* 6h limit with no
margin would otherwise mean a silent overrun.

**Why it was 4x, and why lowering it took measured coefficients.** It was
set against a model that over-predicted a whole run by 3.8x while still
under-predicting that run's slowest worker, so 4x of stated margin was about
1x of real margin, and the two errors cancelling is why nothing looked wrong.
Against the corrected model the same planet run scores 1.06x on the total,
1.27x on a median worker and 0.40x on its worst, so the margin now means
roughly what it says, and the measured per-worker spread above is what 2x is
read from. A lower factor moves this search towards *fewer* workers, but the
search has no term for wall-clock, and wall-clock is what `--worker-scale`
keeps: the run still starts at three workers per lane however much budget is
left. For scale, that planet run's slowest worker sat a factor of **10.5**
under the cap and the whole run used 15.7 of 120 available lane-hours. So
time is not the binding limit today either -- it is simply the only one that
has ever been worth enforcing, and the only one a measured run has never
quietly broken.

`calibrate` still fits a `runner` block from `usage:`'s shard bytes per
output tile, and writes it to `calibration.json`. Sizing reads none of it. It is there to be read by a
person deciding whether a resource has become binding -- which is the
evidence a new limit would be built from, and the reason the figures are
still collected with no limit to spend them on. What `--axis-seconds` feeds
is the *time* model, and only that.

### Worker independence

Every `build-shards` matrix cell is fully independent, by construction:

- Its **only** inputs are its own `manifests/worker-NNN.bin` and the shared,
  read-only `manifests/source.json`. No worker reads another worker's
  manifest, output, or logs.
- There is **no shared mutable state anywhere in the run**: no queue, no
  claim file, no lock, no timing history, no state branch. Nothing a worker
  does is visible to any other worker.
- Its output is one `<basename>-shard-<N>.mbtiles` per profile, named by its
  own index, uploaded under its own artifact name, so two cells can never
  collide on a path. Every profile's file for one worker travels inside that
  one artifact, which is safe because the fixed `-shard-<N>.mbtiles` suffix
  keeps one basename's files out of another basename's per-profile glob in
  `merge`. Producing no file at all, for some or all profiles, is expected
  rather than a failure (an all-ocean slice has no waterway to crop), hence
  the upload's `if-no-files-found: ignore`.
- Its work is fixed before it starts, by `prepare-shards`. A cell computes
  the same result whenever it runs.

So execution order is irrelevant, cells may run concurrently or serially in
any interleaving, and a single failed cell can be re-run on its own without
touching the others. `fail-fast: false` is set for exactly that reason: one
cell failing is not evidence about any other, so the rest are allowed to
finish.

This is the one structural difference from
[TileDistillery](https://github.com/foxandfeature/tiledistillery), which
*does* run a claim queue with shared state on the caller's `state` branch.
The reason is the input, not a difference of opinion: TileDistillery's units
of work are Geofabrik regions, which are named, stable across runs and
wildly uneven in size, so it pays for a queue to get timing history and
longest-first ordering out of it. TileAlchemist slices its own shards out of
the source archive on every run, so a shard has no identity that survives to
the next run, nothing to accumulate history against, and no size skew left
to schedule around: `partition.py`'s `partition_by_cost()` has already
balanced the cells before any of them start. A queue here would add
coordination, shared state, and a failure mode, and buy nothing.

## Publishing

`.github/workflows/_pipeline.yml` is a **reusable** workflow
(`on: workflow_call`) containing only `prepare-shards` → `build-shards` →
`merge`, parameterized by `profile`, `profile_artifact`, `source`, and
`output_basename` (plus `schema`, which only a `static-url` source needs, and
the optional `attribution` template; see "Source resolution" and "Source
attribution"). `profile`/`output_basename` each take
one value (e.g. `profile: ./land.py`) or a comma-separated list
matched 1:1 (e.g. `output_basename: land,cropped-waterways`), so
`prepare-shards` and each worker's fetch happen once per run regardless of
how many profiles are built (see "Fetching"/"Parallelism" above): its
`build-shards` step passes the full profile list to one
`tilealchemist-build-shard` invocation per worker (comma-separated
`--profile`/`--out`, matched 1:1), bundles every profile's shard file for
that worker under one artifact, and its `merge` job matrixes over
`{profile, output_basename}` pairs, each producing its own
`<output_basename>.pmtiles`, uploaded as its own workflow artifact. It
deliberately does **not** publish anywhere, so a third-party caller (see
[`docs/PROFILES.md`](PROFILES.md)) is never forced through this repo's own
credentials. This is a documented, cross-repo, public contract. Callers
reference the `<output_basename>-pmtiles` artifact directly by name
rather than through a job output, since that name is fully deterministic
regardless of profile count, whereas GitHub Actions doesn't guarantee
which matrix cell's value wins for a job-level output across multiple
`merge` matrix cells. Each `merge` cell downloads *every* profile's shard
artifacts and picks its own out by filename prefix, even on a single-profile
run where there is only one set to download. That costs CI-internal
bandwidth and nothing else: the source archive is never touched again after
`build-shards`.

One more input exists for one situation: `artifact_namespace`. The artifacts
the pipeline passes between its own jobs (`shard-manifests`, `shards-<n>`)
are named per pipeline, not per call, which two calls in the *same* workflow
run would collide over — GitHub rejects a second upload of a name already
taken in a run. A caller doing that (this repo's `test.yml`, running the
same profiles against two different sources) names one of them, and its
artifacts become `<namespace>-shard-manifests` and `<namespace>-shards-<n>`.
Callers that call the pipeline once, which is nearly all of them, never set
it.

A prefix rather than a suffix, because the `merge` job globs for the shards
it merges and a prefix is what makes those globs disjoint for free. An
unnamespaced call's `shards-*` cannot reach into `protomaps-shards-0`, so
namespacing the *second* call is enough and the first is left alone; with a
suffix, `shards-*-protomaps` would still have matched a hypothetical third
call's `shards-0-x-protomaps`, and every call would have had to be named to
be safe. It also groups an Actions run's artifact list by call rather than
by artifact kind, which is the more useful order at 128 workers.

The published `<output_basename>-pmtiles` artifacts take no namespace: two
calls in one run need distinct output basenames anyway, or their outputs
would collide by that name instead.

### Getting the profiles in

`_pipeline.yml` never checks the calling repository out. Profiles reach it
as **an artifact the caller uploads before calling it**, named by the
required `profile_artifact` input, which both `prepare-shards` and
`build-shards` unpack at the workspace root so the paths in `profile` read
exactly like repo-relative ones (`./my_profile.py`). `build-shards` runs
them; `prepare-shards` imports them to read each one's `seconds_per_tile`,
which is what makes its prediction profile-specific, so it installs their
PEP 723 dependencies too. It also checks every `profile` path against the
artifact before the walk starts, so a path that isn't there fails once, in
seconds, rather than identically in all `worker_count` build cells after the
archive walk has already run.

The alternative, a bare `actions/checkout` (which inside a called reusable
workflow resolves to the *caller's* repository), would work for a profile
committed to the calling repo and only for that. `build-shards` doesn't need
a repository, it needs one `.py` file; taking that file as an artifact means
it can come from anywhere the calling workflow can produce one: another
repository (this repo's own `test.yml` builds with
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
profiles that way, having none of its own), a generator step, a downloaded
release asset. It also replaces `worker_count` full checkouts with one
upload and `worker_count` downloads of a file measured in kilobytes.

tilealchemist itself is a different matter, and *is* checked out: every job
takes it into `.tilealchemist/` from `job.workflow_repository` at
`job.workflow_sha` (the repository and commit of the workflow file that
defines the job, i.e. this pipeline itself), so the package always matches
the pipeline running it, and a fork picks up its own copy. Those are the
`job` context's properties, not the `github` context's lookalikes, which
inside a called reusable workflow describe the caller instead;
`github.job_workflow_sha` in particular exists only as an OIDC token claim
and is empty in the `github` context
([actions/runner#2417](https://github.com/actions/runner/issues/2417)).

The one place this breaks down is GitHub Enterprise Server, where the
`job.workflow_*` properties are not available at all. An empty `ref` makes
`actions/checkout` fall back to the default branch, which would build
against the wrong tilealchemist commit without any error, so
`prepare-shards` asserts both values are non-empty before its own checkout,
once, in the run's first job, rather than three times over.

A profile's own dependencies then come from the PEP 723 block inside that
profile file, read without importing it; see
[`docs/PROFILES.md`](PROFILES.md). That is also why one file is genuinely
enough to ship through an artifact.

One more reusable workflow handles the half of publishing that is generic.
**`_publish-release.yml`** (`output_basename`, `tag`, `title`, `min_zoom`,
`max_zoom` inputs) downloads one named `<output_basename>-pmtiles` artifact,
splits it into numbered parts if needed, and publishes/replaces a fixed-tag
GitHub Release. It is safe to call cross-repo because it only uses the
automatic `secrets.GITHUB_TOKEN` and `github.repository`/`github.run_number`,
all of which reflect the *calling* repository inside a called reusable
workflow's job, so the release lands in the caller's repo, under the
caller's own token. Its own two mechanics are worth stating, because both
look like arbitrary choices in the workflow file.

### Releasing

The release **tag is fixed and replaced on every run** (`land-latest`, not
`land-2026-09`), so a link to a layer keeps working and never has to be
chased to a new version. Replacing means deleting first: `gh release delete
--cleanup-tag` removes the underlying git tag along with the release, so
tags don't accumulate either. That delete is expected to fail on a
repository's very first run, when there is no prior release to remove, and
is ignored for exactly that reason: the alternative would be a conditional
that is wrong once and pointless forever after.

A finished planet layer routinely exceeds GitHub's per-asset size limit, so
anything over 1900 MiB is **split into numbered `.partNNN` assets**. That
makes reassembly the downloader's problem, which is why the generated
release notes carry a ready-made `gh release download` line for the exact
release: one command fetches every asset at once, whether or not the file
was split, instead of the reader having to work out the part names and
`curl -O` each one. The Windows variant differs only in `copy /b` versus
`cat`.

### Why publishing stops there

Anything needing a credential of its own stays out of this repository, in
the repo that owns that credential. The B2 mirror behind
[tilealchemist-standardprofiles](https://github.com/tilelab/tilealchemist-standardprofiles)'
layers is a plain job in that repository's own build workflow, not a
reusable workflow here, and that is a deliberate structural choice rather
than tidiness.

The reason is that `environment:`-scoped secrets are the one part of the
`github`-context story that does *not* follow the caller: a job's
`environment:` inside a reusable workflow resolves against the repository
that owns the **workflow file**. A public reusable B2 job living here would
therefore hand *this* repository's real B2 credentials to any repository
that called it, and would need a `github.repository ==` job-level `if:`
guard to prevent that, a guard that works, but that only exists to undo a
problem created by putting the job in the wrong repository. A job in the
repository that owns the credentials needs no guard at all, only its own
environment's approval gate.

The same reasoning is why `_pipeline.yml` publishes nowhere: a caller's
`environment:`/secrets have to resolve against the caller.

Every `publish-*` job in a caller's workflow `needs:` the job that calls
`_pipeline.yml`, the one real cross-job dependency; everything else flows
through `inputs.*` or named artifacts. A third-party repo calls
`_pipeline.yml` via
`uses: tilelab/tilealchemist/.github/workflows/_pipeline.yml@<ref>`,
a native cross-repo capability of reusable workflows, no GitHub Marketplace
listing required.

## Module invariants

Facts that reach across modules, which no single docstring is the right
home for. Each is a constraint an edit could break silently, so change the
code and this section together.

### Phase maps

`prepare-shards`, driven by `shard_prep.run_prepare()`:

    run_prepare()
      resolve_source()                sources/: which archive to read
      make_session()                  ranged_fetch.py: shared by every fetch
      collect_entries()               pmtiles_index.py: header + index in 2 requests
      fetch_declared_attribution()    attribution.py: what the archive credits
      compose_attribution()           attribution.py: what this layer credits
      compute_gaps()                  pmtiles_index.py: tile_ids no entry covers
      _size_run()                     sizing.py: how many workers, and their blocks
      write_worker_manifests()        manifest.py: worker-NNN.bin
      write_source_metadata()         manifest.py: source.json, shared

`build-shard`, driven by `shard_worker.run_worker()`:

    run_worker()
      read_source_metadata()     manifest.py: source.json from prepare-shards
      read_manifest()            manifest.py: this worker's entries
      split_manifest_entries()   -> real entries / gap entries
      init_mbtiles()             mbtiles.py: one ShardWriter per profile
      real entries (_process_real_entries), one batch at a time:
        plan_fetch_batches()       fetch_batching.py: one range GET per batch
        fetch_batch_blob()         fetch_batching.py: that batch's bytes
        run_transform()            transform.py: a chunk of tiles at a time
        ShardWriter.write()        mbtiles.py: that chunk, then drop it
      gap entries (_process_gap_entries):
        Profile.transform_gap()    one blob for every gap tile in the run
        write_gap_tiles()          mbtiles.py: nothing to fetch
      close_shards()
      _report_worker_usage()     usage.py: one line per profile, one per worker

### Zoom levels (`zoom.py`)

A zoom is a `ZoomLevel` member everywhere the pipeline passes one around, so
a level outside the set raises `ValueError` where it enters rather than
walking to nothing. `IntEnum`, because a zoom level *is* a number wherever
the pipeline computes with it — `zxy_to_tileid(max_zoom + 1, ...)`, the
`min_zoom <= zoom <= max_zoom` filter, the `2 ** zoom` row flip.

`MAX_SUPPORTED_ZOOM = 30` is a hard ceiling, not a preference:
`zxy_to_tileid()` raises `OverflowError` above z31 because `tile_id` stops
fitting a 64-bit int, and `tile_id_bounds()` always asks it for
`max_zoom + 1`. The members are generated through the functional API with
`module=`/`qualname=` set, which is what makes them picklable — `ChunkJob`
carries one into a transform pool worker.

### The manifest format (`manifest.py`)

One file per worker, a flat sequence of fixed-size records, no framing: file
size / `RECORD.size` gives the count.

    tile_id: uint64, offset: uint64, length: uint32, run_length: uint32

These mirror a PMTiles directory entry. `source.json` carries what is true
for the whole run, and its JSON key strings stay confined to
`as_json()`/`from_json()`: every other reader names a field, so a renamed or
missing key is a mistake at the two ends of the file format rather than a
`KeyError` wherever a worker happens to look something up.

### Tiles carry no coordinate (`tile.py`)

Everything on a `Tile` is tile-local, hence identical for every z/x/y that
dedupes to the same bytes. Three things rest on that invariant: one object
standing for a whole `run_length` run; one standing for two entries pointing
at the same `(offset, length)`, which offset-ordered batching makes adjacent;
and one `Tile.empty()` serving a hundred-thousand-tile gap region.

`extent` reads the first layer's. MVT allows one extent per layer, but a tile
in practice encodes every layer at the same one, and a tile with no layers
falls back to the schema's `default_extent`.

### Encoding (`mvt.py`, `mbtiles.py`)

`gzip.compress(..., mtime=0)` is required, not tidiness. Without it gzip
embeds the current time, so byte-identical tile content — every gap tile, in
particular — compresses differently across worker processes and defeats
PMTiles' content-hash dedup in the final merge.

mbtiles numbers rows TMS-style while a PMTiles tile ID decodes to XYZ, so
every write flips the row (`(2 ** zoom - 1) - tile_row`).

The transform hands the writer *runs*, `(tile_id, run_length, output_data)`,
rather than one tuple per tile, so `mbtiles.py` is where a run becomes rows.
`_tile_rows()` must stay a generator: a worker holding a million-tile run (an
ocean, an ice sheet interior) would otherwise materialize them all as one
list before sqlite3 sees them. `_run_counts()` walks the runs rather than the
rows, which is what lets the counting and that generator coexist.

A run is clipped to the zoom range in `transform_batch_blob_multi()`, and
*before* the `None` test, because the per-tile filter it replaces ran there
too: a tile outside the range appears in **neither** `written` nor `skipped`.
Clipping there is exact rather than approximate, because PMTiles tile IDs are
ordered by zoom -- `min_zoom <= zoom <= max_zoom` across a run selects the
same tiles as intersecting that run with `tile_id_bounds(min_zoom, max_zoom)`.
That makes the writer the third consumer of one derivation instead of a
fourth reimplementation of it, and turns an O(run_length) filter into O(1)
arithmetic.

### The directory walk (`pmtiles_index.py`)

`leaf_window_for()` must prune by exactly the rule `walk_directory_tree()`
descends by. The two are kept in step by hand, because the walk pays that
rule per entry over a planet's worth of directories while the window pays it
over the root's few thousand. Leaf directories sit in the file in
root-pointer order, so the ones the walk reaches run from the first match to
the last; an archive ordering them otherwise trips `LeafWindow.node_bytes()`'s
bounds check, which is all that stands between an unexpected layout and a
silently short slice decoding into plausible-looking garbage.

`tile_id_bounds()` is derived in one place because the walk prunes against
those bounds and `compute_gaps()` fills the untouched stretches between
entries — the two must agree exactly.

`WalkProgress`'s percentage divides by the leaf window's length, which is an
upper bound: tile_id pruning lets the walk finish without decoding the whole
window, so the percentage can stop short of 100%.

### Retries (`ranged_fetch.py`)

Retries cover the transient ways a CDN fails under a cold-cache stampede,
many concurrent workers hitting a freshly-published archive at once. Three
shapes, none of them permanent:

- a 200 instead of a 206 — the server ignored the `Range` header, and reading
  the response in full would be tens of GB;
- a 429/5xx — rate-limiting, or buckling under the burst;
- the connection dropping mid-stream, seen as an `IncompleteRead` well past
  the halfway point of a large batch. There is no response left to read a
  `Retry-After` from, so the backoff runs on jitter alone.

`on_chunk(bytes_so_far)` receives the running total for the *current*
attempt, not a delta, so a retry that restarts the transfer rewinds the
progress line instead of counting the re-sent bytes twice.
`_warn_retry()` emits a `::warning` workflow command as the retry happens, so
a stampede surfaces in the Actions UI and not only in the job log.

### Batching and chunking (`fetch_batching.py`, `transform_pool.py`)

Entries arrive sorted by offset, but sorted is not adjacent: dedup points an
entry at whatever tile first held its bytes, so two neighbours can sit
gigabytes apart with data this run never reads in between. One GET across
such a hole would download all of it, so the manifest is split at every hole
wider than `--max-fetch-gap`. Two entries at the same offset are zero apart
and must not be split. The 8 MB default is where one request still beats two:
a few MB of unread bytes on an open, streaming connection cost less than
another round trip against a cold CDN.

`peak_batch_bytes()` lives here rather than with the planner that calls it,
because it is the same rule read the other way: it walks a block and reports
the widest span `plan_fetch_batches()` would produce, without building the
batches. Split the two across modules and one of them drifts.

`_chunk_entries()` must keep chunks contiguous and in order.
`_blob_slice_for_chunk()` slices one byte range per chunk, and
`transform_batch_blob_multi()`'s dedup compares only against the previous
entry, so a duplicate pair split across a chunk boundary misses that one
dedup — harmlessly, but only because the chunks are contiguous.

`_transform_chunk()` rebuilds two things that cannot cross a process
boundary: the profiles, which the pickler cannot reconstruct in a worker at
all, reimported once per chunk rather than per tile; and its
`TransformProgress`, which holds a `threading.Lock`. Only that object is
unpicklable, not the reporting — the interval is a plain float, so a worker
throttles its own lines over its own chunk while the parent's "chunk N done"
lines carry the whole-shard view. The schema crosses as its `SchemaName`, and
the child looks the same singleton up out of `SCHEMAS`.

In `_pooled_chunk_results()`, `pending.pop(future)` must pop rather than
index. A `Future` keeps the result it was handed for as long as the `Future`
itself is alive, so holding every entry until the pool shuts down would pin
every chunk's output for the whole transform phase — exactly the memory
`run_transform()` yields per chunk to avoid. For the same reason it must
keep submitting lazily: `executor.submit()` hands the work queue a *copy* of
that chunk's bytes, so submitting every chunk up front holds the batch
twice.

`transform.py` is the transform itself and nothing else; `transform_pool.py`
is the chunking and the process wiring around it. The dependency runs one
way — the pool imports the transform — which is also why `run_transform()`
lives on the pool side despite having an inline branch: it chooses between
the two strategies.

### Profiles and gaps

`load_profile()` deliberately does not register the module in `sys.modules`.
Transform pool workers reload profiles by path, which works under both fork
and spawn; registering would only help under fork.

`compute_gaps()` tags a gap record `length=0`, the sentinel
`split_manifest_entries()` tells a gap by, there being nothing to fetch.
`GAP_CHUNK_SIZE` caps one such record so that a single huge unbroken gap — a
whole ice sheet's interior — cannot land entirely on one worker.
