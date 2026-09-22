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
   the run's flow, `pmtiles_index.py` for the walk itself, `partition.py`
   for the split that follows) walks the PMTiles directory tree (root +
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
   `--worker-count` (the reusable pipeline's `worker_count` input, default
   128) **contiguous** chunks, one per worker (`tilealchemist/partition.py`,
   written out by `tilealchemist/manifest.py`). Offset order tracks tile-ID
   order almost everywhere, but also catches what tile-ID order misses: an
   entry that dedupes against a *non-adjacent* tile with identical bytes
   (e.g. the same "all water" tile recurring across different oceans) lands
   in the same worker as the tile it's deduped against, instead of a random
   other worker re-fetching the same bytes. `partition.py`'s
   `partition_by_cost()` keeps a run of same-offset entries whole across
   worker boundaries, past a worker's target size, but only up to one whole
   share of the run's cost (see "Parallelism" for what that weighs). That
   cap is not a detail: a Protomaps planet build dedupes its open ocean into
   same-offset runs of hundreds of thousands of entries (315K and 306K at
   z0..z11 alone, against a 128-worker share of 16K), and an unbounded rule
   drops every one of them on a single worker however high `worker_count`
   goes, while starving the workers after it. Splitting such a run costs
   only what keeping it whole was buying — one tile's bytes re-fetched per
   extra worker — so the cap is the cheap side of that trade.
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
   `partition.py`'s `compute_gaps()` finds and chunks these, tagged with a
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
keeps each job smaller: that's why the default is 128 rather than higher,
and why `--worker-count auto` only ever considers multiples of that
concurrency (see "Sizing a run"). Each worker writes its own small mbtiles
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
`partition_by_cost()`, the same function that splits work across workers,
deliberately producing several times more chunks than there are processes
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
  for is an explicit cap now (see "Budgets are caps, not prices").
- **Per output tile**, one sqlite insert per tile per profile.

`cost_weights()` therefore returns a predicted *duration* rather than a
share of something: `partition_by_cost()` splits on that number, and
`prepare_shards` prints the run's predicted core-hours and slowest worker.

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

### What the model still cannot see

Its R² against those durations caps out at **0.29**, and it under-predicts
the slow tail by 2-4x: the worst worker was predicted at 8m and ran 34m12s.
What is missing is content complexity. A dense coastline tile costs far
more to clip and union than an open-ocean tile of the same byte length, and
nothing in a manifest record exposes that -- the same effect the
over-chunking below exists to absorb. Treat the printed prediction as a
ranking signal, not an estimate.

Two further cautions on the fit. The run it is fitted against was itself
partitioned by the normalized model, so the predictors are correlated by
construction (bytes against output tiles at -0.80), which is why the
regression alone cannot pin the small coefficients and the direct
observations above carry them instead. And `DENSITY_EXPONENT` barely earns
its keep: sweeping it from 1.0 to 2.0 moves the correlation between 0.499
and 0.521, peaking around 1.4-1.5. It stays at 1.5 because that is the
flat top of the curve, not because the data insists on it.

### Measuring a run

Every coefficient above is a guess until something measures it, so a worker
reports what it actually did. `usage.py` prints one `usage:` line per scope,
in `name=value` form: a whole run's budget is one `grep '^usage:'` over the
job logs, and that grep is what the next run's coefficients are fitted from.

Three scopes:

- **`scope=chunk`**, one per transform chunk, printed by the process that ran
  it. `TRANSFORM_CHUNKS_PER_WORKER = 8` on 4 vCPU makes that ~32 lines per
  worker rather than one, so a 128-worker run yields ~4,096 measurements
  instead of 128. Chunks also vary far more in composition than the
  deliberately equal-cost worker blocks do, which is what breaks the
  collinearity a worker-level regression cannot get past (bytes against
  output tiles at -0.80, by construction, because the run was partitioned by
  the model being fitted). A chunk measurement contains no runner boot and no
  `pip install` either.
- **`scope=profile`**, one per profile per worker: `written`, `skipped`,
  `blobs`, and the finished shard's size on disk. `blobs` counts *distinct*
  blob objects rather than rows, so `written / blobs` is the storage
  amplification -- the number that decides whether the deduplicated shard
  layout pays for itself (see "Shard layout").
- **`scope=worker`**, one per worker: wall-clock seconds split by phase
  (`fetch`, `transform`, `write`, `close`), bytes fetched, entry counts, and
  peak RSS. `PhaseSeconds` nests exclusively, so the `write` time spent
  inside the transform loop is not also counted as `transform`.

`decode_seconds` and `transform_seconds` are reported separately because they
are different work on different inputs. `Tile.decode()` is gunzip plus
protobuf, close to linear in byte length, paid once per *distinct* entry;
`profile.transform_tile()` is shapely clip and union, paid once per profile
and driven by content complexity rather than byte length. Fused into one
number they are indistinguishable, and an R² of 0.29 cannot be read as either
"the model is bad" or "decode is clean and all the variance sits in
transform". The split also makes profile count a real factor: a run costs
`1 x decode + N x transform`, which one fused measurement cannot express.

`length_hist` accumulates the same two times into log2 buckets of entry
length (bucket `i` holds lengths whose bit length is `i`, i.e.
`[2**(i-1), 2**i)`), as `bits:count:bytes:decode_seconds:transform_seconds`.
Buckets rather than 43M individual samples: the curve is what says whether
`DENSITY_EXPONENT` is right, and it fits in a log line.

The instrumentation is two `perf_counter()` calls per distinct entry. At 26ns
a call that is 2.3s across the reference run's 43,272,366 distinct entries --
0.004% of its 15.7 core-hours.

Peak RSS is read twice, because `RUSAGE_CHILDREN.ru_maxrss` is the *maximum*
over finished children, not their sum: taken alone it under-reports a pooled
worker's real peak by up to `--transform-workers`x. Each `_transform_chunk`
therefore prints its own `RUSAGE_SELF` peak on the way back, which is what
makes the *simultaneous* per-process peak visible. `getrusage` reports
`ru_maxrss` in bytes on macOS and in kibibytes on Linux, so `usage.py` scales
by platform; every `usage:` byte figure is bytes.

`WORKER_SETUP_SECONDS` sits next to `AXIS_SECONDS` and is a property of the
runner rather than of the run: runner boot, artifact download, and the
`pip install` of the dependencies. It does not change partitioning -- a
constant every worker pays alike cancels out of every ratio -- but it changes
two things that matter. The printed core-hours stop hiding it inside the byte
terms (128 x 39s is 1.4 of the reference run's 15.7 core-hours, about 9%),
and the `worker_count` search has to weigh it, every extra worker bringing
its own setup with it.

### Budgets are caps, not prices

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

So the limits are limits now. `partition_by_cost()` takes `Caps` and closes a
block the moment another group would break one, whatever the cost balance
says -- one condition in the loop beside the existing `_share_end` comparison.
Exact instead of statistical, and `manifest_record` goes back to its measured
~1e-6, which leaves the cost model with no coefficient in it that is not a
measurement.

Two caps, both from `budgets.py`:

- **Records per block**, from `--manifest-ram-budget` at a measured ~120 B per
  `Entry`: 0.25 GB is 2.2M records, 0.5 GB (the default) 4.5M, 1.0 GB 8.9M.
- **Peak batch bytes**, from `--peak-batch-budget`. The peak, deliberately,
  not the block's byte sum: `_process_real_entries()` does `del blob` between
  batches, so what a worker must afford at once is its largest *batch*. That
  is not a model but an arithmetic fact, and `budgets.py` tracks it by the
  same rule `plan_fetch_batches()` splits on, incrementally as groups are
  added rather than by re-planning the block each time.

A record cap can also split an *atomic* group, and has to. `atomic_key`
groups a run of records sharing an `(offset)`, and such a run can be larger
than the whole cap on its own -- one real worker held 1,449,554 records that
deduped to a single distinct entry. `_atomic_groups()` already splits such a
run once it outgrows a share; the cap is a second reason to split, and the
cost of splitting is one extra decode, because the dedup check only ever
compares against the previous entry anyway.

What a cap cannot do is invent workers. The last block takes whatever is
left, however big, so `prepare-shards` says out loud when a block overran and
names raising `--worker-count` as the fix -- which is the job the
`worker_count` search does automatically.

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

Free disk space is checked before every batch (`usage.free_disk_bytes()`)
and reported on the worker's `usage:` line. Falling below the threshold is a
loud warning, not an abort: the approach shows up in the log instead of
arriving as an `ENOSPC` in the middle of an `INSERT`.

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
| `output_tile` | `sum(write_seconds) / sum(output_tiles)` |
| `decode_call`, `decoded_byte` | two-parameter least squares over the `length_hist` buckets, against `(count, count * mean_length ** DENSITY_EXPONENT)` |
| `manifest_record` | the slope of `setup_seconds` against a worker's record count |

Every aggregate is `sum(seconds) / sum(units)`, never the mean of per-unit
rates. That is the same choice tiledistillery's `fit_seconds_per_byte()`
documents: small units carry a fixed overhead, so averaging their rates lets
the smallest ones dominate. Here that would let the worker holding one
distinct entry weigh as much as the one holding 2M.

The entry-cost fit targets `decode_seconds + transform_seconds` together,
because the model charges both to the same per-distinct-entry axes. Keeping
them apart in the *measurement* is what makes the fit reviewable: the
proposal prints the split, so "the model is bad" and "decode is clean, the
variance is all in transform" stop looking alike. The same buckets sweep
`DENSITY_EXPONENT` and print which exponent the length curve actually
prefers -- the documented 1.5 sits on the flat top of that curve, not on a
value the data insists on.

This does not port tiledistillery's approach, and it is worth saying why.
`leaves.py` looks each Geofabrik region up in `timings.json`, where a
measurement *beats* the model; the fitted `seconds_per_byte` exists only to
place regions that were never built. tilealchemist's unit of work is a
worker block recomputed every run, so `worker-042.bin` means something
different next time and there is no key to hang a measurement on. Only the
*coefficients* carry over.

Four guards, because a bad calibration does its damage quietly, inside
`partition_by_cost()` on every later run:

- **Every worker must have reported.** A partial run is a biased sample --
  the workers that failed are exactly the expensive ones.
- **Each coefficient is clamped** to within `CLAMP_FACTOR` of the reviewed
  one, and every clamp is printed as a warning naming the raw measurement.
- **With `--manifest-dir`**, the proposal is scored against the run's own
  measured worker durations alongside the reviewed coefficients. Ranking
  worse than what it would replace is a warning that says not to commit it.
  Below `MIN_SCORED_WORKERS` the score is withheld rather than printed: over
  two points a correlation is always exactly +/-1, which would read as a
  verdict while meaning nothing.
- **Nothing is written automatically.** `--out` writes a `calibration.json`
  for a caller to keep; committing it, or passing it to
  `prepare-shards --axis-seconds`, is a human decision. The model shapes the
  distribution it is then fitted on, which is why two of the current five
  coefficients had to be set by hand from single observations in the first
  place.

`worker_setup_seconds` is deliberately not learned from logs alone. A
worker's own `setup_seconds` is the *in-process* residual (wall clock minus
the phases); runner boot, artifact download and `pip install` happen before
the process exists and no line in its log can see them. The proposal
therefore leaves it alone unless `--runner-overhead-seconds` supplies that
half, and says so.

The coefficients are profile-dependent -- `_entry_outputs()` runs
`transform_tile()` once per profile, so the seconds per byte belong to
`land` + `cropped_waterways` rather than to tilealchemist. A single constant
in the library cannot be right for every caller at once, which is why the
calibration is a file, keyed by the caller on
`(output_basename, schema, source.build)` the way tiledistillery keys
`timings.json` by region, and kept in the caller's state rather than here.

What makes the fit identifiable in the long run is runs of *different shape*
-- planet, a regional extract, a high-zoom range. That mixture is exactly
what the earlier normalized model could not survive ("a z0..z11 ocean-heavy
range is almost all sqlite inserts, a high-zoom extract is almost all
decode"), which makes it the training set that separates the coefficients
rather than the one that confounds them.

### Sizing a run

`--worker-count auto` lets `prepare-shards` pick the count instead of taking
it as an input. The pipeline was already built for it by accident:
`_pipeline.yml` generates the worker matrix in `gen-workers` *after* the
archive walk, in the same job, so the manifests exist by then. `gen-workers`
counts `manifests/worker-*.bin` rather than reading the input back, which
makes the manifests the only truth about how many workers there are.

Partitioning is one pass over the records, so the search needs no closed
form:

    for N = C, 2C, 3C, ...:                 # C = --concurrency, in practice 20
        blocks = partition_into_worker_blocks(entries, gaps, N)
        if every budget holds for the worst block: take N

Three budgets, three sources. **Time** from `cost_weights()` against the 6h
job cap. **RAM** from the block's peak *batch* bytes through a measured
affine fit. **Disk** from its output tiles times measured bytes per tile.
All three fall as N rises -- more workers, smaller blocks, smaller spans,
smaller batches -- and only setup overhead and wave count rise, so the first
N that fits is the best one and the search is a loop rather than an
optimization. Verified: against a 1.5M-entry, 77 GiB synthetic manifest the
chosen N falls 120 -> 60 -> 40 -> 20 as the RAM budget rises 4 -> 8 -> 14 ->
28 GiB, and tightening the job budget from 6h to 2h at 14 GiB raises it back
to 60, with the log naming which budget bound it at each step.

**Only multiples of the concurrency are considered.** At 20 lanes and
roughly equal-cost blocks a run goes in waves:

| workers | waves | last wave |
| --- | --- | --- |
| 120 | 6 | 20/20 |
| **128 (the old default)** | **7** | **8/20** |
| 140 | 7 | 20/20 |

128 buys the same seven waves as 140 and leaves 12 lanes idle in the last
one; 120 is the same work in six. It is an idealization -- GitHub starts
greedily as a lane frees rather than in strict waves -- but it is measurable
and it is the cheapest saving available.

`worker_count` also has a hard ceiling that nothing used to check:
`_pipeline.yml` expands it straight into the `build-shards` matrix, and
GitHub refuses more than **256** cells. Both `prepare-shards` and
`gen-workers` reject it now, the latter before the matrix is built rather
than after the archive walk has already run.

The time budget is charged at `TAIL_SAFETY_FACTOR` (4x), not at face value,
and that is the honest way to use a model that under-predicts its slow tail
by 2-4x -- the reference run's worst worker was predicted at 8m and ran
34m12s. Sizing against a *hard* 6h limit with such a model would otherwise
mean either a blind guess or a silent overrun. Which is also why this comes
last: it needs measured coefficients to mean anything. For scale, that run's
slowest worker sat a factor of **10.5** under the cap and the whole run used
15.7 of 120 available lane-hours. Time is not the binding limit today; RAM
and disk are.

The RAM and disk conversions are measured rather than assumed.
`usage:`'s `peak_rss` and `peak_batch_bytes` give an affine fit of peak
memory against the largest batch a worker fetched, and the `profile` lines'
`shard_bytes` over `output_tiles` gives bytes per tile. `calibrate` emits
both as the `runner` block of `calibration.json`, and `--axis-seconds` reads
them back. Until a run has measured them, `DEFAULT_RUNNER` carries the one
observation there was: 3.49 GB of tile bytes dying on a 16 GB runner, so at
least 4.6x.

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
required `profile_artifact` input, which `build-shards` unpacks at the
workspace root so the paths in `profile` read exactly like repo-relative ones
(`./my_profile.py`). `prepare-shards` downloads it too, but only to fail a
`profile` path that isn't in the artifact once, in seconds, rather than
identically in all `worker_count` build cells after the archive walk has
already run.

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

Facts the code depends on that the code itself cannot state. They lived in
comments before [`COMMENT_STYLE.md`](COMMENT_STYLE.md) moved them here; each
is a constraint an edit could break silently, so change the code and this
section together.

### Phase maps

`prepare-shards`, driven by `shard_prep.run_prepare()`:

    run_prepare()
      resolve_source()                sources/: which archive to read
      make_session()                  ranged_fetch.py: shared by every fetch
      collect_entries()               pmtiles_index.py: header + index in 2 requests
      fetch_declared_attribution()    attribution.py: what the archive credits
      compose_attribution()           attribution.py: what this layer credits
      compute_gaps()                  partition.py: tile_ids no entry covers
      caps_from_budgets()             budgets.py: this run's hard per-worker limits
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
      _report_worker_usage()     usage.py: one `usage:` line per scope

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
