"""The run's one-time planning step; see docs/ARCHITECTURE.md "Fetching"."""
import os
import sys

from tilealchemist import axis_state, block_state
from tilealchemist.attribution import compose_attribution, fetch_declared_attribution
from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_model
from tilealchemist.manifest import axis_key_for, write_source_metadata, write_worker_manifests
from tilealchemist.partition import count_gap_tiles, count_output_tiles, tile_block_groups
from tilealchemist.sizing import block_loads, breaches, choose_worker_count, worst_of
from tilealchemist.pmtiles_index import collect_entries, compute_gaps
from tilealchemist.ranged_fetch import make_session
from tilealchemist.sources import resolve_source
from tilealchemist.tile_blocks import TILE_BLOCK_BITS, profile_combo_key


def run_prepare(args):
    """Plan the whole run, once, before any worker starts.

    Walks the archive's directory tree, settles the attribution, sizes the
    run and writes one manifest per worker. The layer's attribution goes to
    stdout for the pipeline to hand on, and every log line to stderr.

    Args:
        args: The parsed command line from prepare_shards.py.

    Raises:
        ValueError: If the run would produce a layer crediting nobody, which
            is checked before the manifests are written rather than at merge
            time.
    """
    os.makedirs(args.out_dir, exist_ok=True)

    resolved_source = resolve_source(args.source, args.source_url, args.schema).resolve()
    print(f"source={resolved_source.url} (build {resolved_source.build}, "
          f"schema {resolved_source.schema.name})", file=sys.stderr)

    model, setup_seconds = _settle_costs(args, resolved_source)

    session = make_session()
    header, entries = collect_entries(session, resolved_source.url,
                                       args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})", file=sys.stderr)

    declared = fetch_declared_attribution(session, resolved_source.url, header)
    print(f"source attribution: {declared or 'none declared by the archive'}",
          file=sys.stderr)
    # Before the manifests: an unattributable run stops here, not at merge time.
    attribution = compose_attribution(declared, args.attribution)

    gaps = compute_gaps(entries, args.min_zoom, args.max_zoom)
    gap_tile_count = sum(gap.run_length for gap in gaps)
    print(f"{len(gaps)} gap ranges covering {gap_tile_count} tiles with no archive "
          f"entry at all", file=sys.stderr)

    groups = tile_block_groups(entries, model)
    measured_count = sum(1 for block in groups.blocks if block in model.block_seconds)
    largest = max(groups.seconds, default=0.0)
    print(f"{len(groups.blocks)} tile blocks of up to {2 ** TILE_BLOCK_BITS} tiles, "
          f"{measured_count} of them costed from their measured profile seconds and the rest "
          f"from the model; "
          f"the costliest is {largest / 60:.1f}m, which no worker's share can go below",
          file=sys.stderr)

    worker_count, blocks = _size_run(args, entries, gaps, model, setup_seconds, groups)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom, args.max_zoom,
                          header["tile_data_offset"])

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)
    print(f"largest block holds {max(len(block) for block in blocks)} records", file=sys.stderr)
    print(_tiles_line(blocks), file=sys.stderr)
    loads = block_loads(blocks, model, setup_seconds)
    _print_worker_predictions(loads, args.limits)
    load = worst_of(loads)
    broken = breaches(load, args.limits)
    print(f"worst worker: {load.seconds / 60:.0f}m predicted, {load.tiles} output tiles, "
          f"largest batch {load.batch_bytes / 2 ** 30:.2f} GiB", file=sys.stderr)
    if broken:
        print(f"::warning title=worker budget::the worst worker is over budget on "
              f"{', '.join(broken)} at {worker_count} workers", file=sys.stderr)
    worker_seconds = [worker_load.seconds for worker_load in loads]
    even_minutes = sum(worker_seconds) / len(blocks) / 60
    print(f"cost model predicts {sum(worker_seconds) / 3600:.1f} core-hours including "
          f"{setup_seconds:.0f}s setup per worker, slowest worker "
          f"{max(worker_seconds) / 60:.0f}m against an even {even_minutes:.0f}m",
          file=sys.stderr)
    # stdout carries the attribution alone, for _pipeline.yml to hand to tile-join.
    print(attribution)


def _print_worker_predictions(loads, limits):
    """Say what each worker is predicted to cost, one line per manifest.

    The summary lines below say what the worst and the average worker come to,
    which is what the sizing decision turns on, but not which worker is which.
    A shard that overruns, or one that finishes in seconds, is only findable
    from its own prediction, so every manifest gets its line, named as the file
    it was written to and the matrix cell that will read it. The budget share
    is the figure the limits are actually judged on: predicted seconds charged
    at the tail factor, against `--job-seconds`.

    A planet run has as many of these lines as it has workers, so they go in a
    folded group: `::group::` is the same Actions annotation as the warnings
    around them, and a terminal shows it as the plain line it is.

    Args:
        loads: One BlockLoad per worker, in worker order.
        limits: The run's hard limits, for what share of its budget each
            prediction spends.
    """
    print(f"::group::predicted per worker ({len(loads)} manifests)", file=sys.stderr)
    for worker_index, load in enumerate(loads):
        budget_share = load.seconds * limits.tail_factor / limits.job_seconds
        print(f"worker-{worker_index:03d}: {load.seconds / 60:6.1f}m predicted, "
              f"{load.tiles} output tiles, {load.records} records, "
              f"{budget_share:.0%} of budget", file=sys.stderr)
    print("::endgroup::", file=sys.stderr)


def _settle_costs(args, resolved_source):
    """Work out what this run will be charged, and keep it here.

    Everything is settled together because it is one decision: the five
    per-axis seconds, what each profile costs, what one worker's process pool
    buys, and what a worker costs before it starts. Where the state branch has
    recorded runs, each figure is the median of them, which one odd run cannot
    move far on its own. Where it has none, the reviewed constants and the
    profiles' own declared estimates stand.

    A gap tile's weight is settled by measurement either way, by asking each
    profile once before any network work happens.

    The result is one CostModel, and every later step takes that one object:
    partitioning and sizing used to be handed the axes and the profile costs
    separately, and partitioning quietly went without the second.

    The profiles' seconds per tile block are settled here too, from the
    block state for this archive and profile set, where there is any.

    Args:
        args: The parsed command line, read for its profiles, its state
            document, its block state and any `--axis-seconds` override.
        resolved_source: The archive this run will read.

    Returns:
        The CostModel this run is priced by, and the per-worker setup seconds.
    """
    document = args.axis_state_document
    notes = []
    axis, setup_seconds, parallelism = args.axis_seconds, WORKER_SETUP_SECONDS, None
    if document is not None:
        source_key = axis_key_for(resolved_source.url, resolved_source.schema.name)
        axis, shared = axis_state.axis_seconds(document, source_key, notes)
        setup_seconds = shared.worker_setup_seconds
        parallelism = shared.transform_parallelism
        print(f"axis state: {source_key} costed from "
              f"{axis_state.history_depth(document)} recorded run(s) of "
              f"{axis_state.HISTORY_LENGTH}, each coefficient the median of its own",
              file=sys.stderr)
    profile_costs, lines = axis_state.settle_profile_costs(
        document if document is not None else axis_state.empty_document(),
        args.profiles, resolved_source.schema, notes)
    for line in lines:
        print(f"costing: {line}", file=sys.stderr)
    for note in notes:
        print(f"::warning title=axis state::{note}", file=sys.stderr)
    block_seconds = {}
    if args.block_state and args.profiles:
        source_key = axis_key_for(resolved_source.url, resolved_source.schema.name)
        profiles_key = profile_combo_key(profile.name for profile in args.profiles)
        block_seconds = block_state.read_block_medians(args.block_state, source_key,
                                                       profiles_key)
        print(f"block state: {len(block_seconds)} tile blocks measured for "
              f"{source_key}/{profiles_key}, each the median of up to "
              f"{axis_state.HISTORY_LENGTH} runs", file=sys.stderr)
    model = cost_model(axis=axis, profiles=profile_costs, transform_parallelism=parallelism,
                       block_seconds=block_seconds)
    print(f"costing: one worker's pool buys {model.transform_parallelism:.2f}s of decode and "
          f"profile work per second of its wall clock", file=sys.stderr)
    return model, setup_seconds


def _tiles_line(blocks):
    """Say what the worst block writes.

    Args:
        blocks: One entry block per worker.

    Returns:
        That line, ready for stderr.
    """
    worst = max(blocks, key=count_output_tiles)
    tiles, gap_tiles = count_output_tiles(worst), count_gap_tiles(worst)
    return (f"worst block: {len(worst)} records writing {tiles} output tiles "
            f"({gap_tiles} of them gap tiles)")


def _size_run(args, entries, gaps, model, setup_seconds, groups):
    """The worker count this run sized itself to, and its blocks.

    Args:
        args: The parsed command line, for the limits.
        entries: The archive's directory entries for this run.
        gaps: The gap records covering what the archive does not hold.
        model: The CostModel this run is priced by.
        setup_seconds: What a worker costs before it reaches its first record.
        groups: The entries' TileBlockGroups under that model.

    Returns:
        The chosen worker count and its blocks. Every count tried is logged,
        so the log says which limit pushed the run to the count it landed on.
    """
    worker_count, blocks, _load, attempts = choose_worker_count(
        entries, gaps, model, args.limits, setup_seconds=setup_seconds, groups=groups)
    for tried, load, broken in attempts:
        verdict = f"{', '.join(broken)} over budget" if broken else "fits"
        print(f"sizing: {tried} workers, worst worker {load.seconds / 60:.0f}m predicted, "
              f"{load.tiles} output tiles -- {verdict}", file=sys.stderr)
    return worker_count, blocks
