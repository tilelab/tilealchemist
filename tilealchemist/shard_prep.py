"""The run's one-time planning step; see docs/ARCHITECTURE.md "Fetching"."""
import os
import sys

from tilealchemist import axis_state, block_state
from tilealchemist.attribution import (
    compose_attribution,
    fetch_declared_attribution,
)
from tilealchemist.cost import WORKER_SETUP_SECONDS, ProfileCost, cost_model
from tilealchemist.manifest import (
    axis_key_for,
    write_source_metadata,
    write_worker_manifests,
)
from tilealchemist.partition import (
    count_gap_tiles,
    count_output_tiles,
    tile_block_groups,
)
from tilealchemist.pmtiles_index import collect_entries, compute_gaps
from tilealchemist.ranged_fetch import make_session
from tilealchemist.sizing import (
    block_loads,
    breaches,
    choose_worker_count,
    worst_of,
)
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

    resolved_source = resolve_source(args.source, args.source_url,
                                     args.schema).resolve()
    print(f"source={resolved_source.url} (build {resolved_source.build}, "
          f"schema {resolved_source.schema.name})", file=sys.stderr)

    model, setup_seconds = _settle_costs(args, resolved_source)

    session = make_session()
    header, entries = collect_entries(session, resolved_source.url,
                                       args.min_zoom, args.max_zoom)
    print(f"directory walk found {len(entries)} distinct tile entries "
          f"(min_zoom={args.min_zoom}, max_zoom={args.max_zoom})",
          file=sys.stderr)

    declared = fetch_declared_attribution(session, resolved_source.url, header)
    print(f"source attribution: {declared or 'none declared by the archive'}",
          file=sys.stderr)
    # Before the manifests: an unattributable run stops here, not at merge time.
    attribution = compose_attribution(declared, args.attribution)

    gaps = compute_gaps(entries, args.min_zoom, args.max_zoom)
    gap_tile_count = sum(gap.run_length for gap in gaps)
    print(f"{len(gaps)} gap ranges covering {gap_tile_count} tiles with no "
          f"archive entry at all", file=sys.stderr)

    groups = tile_block_groups(entries, model)
    measured_count = sum(1 for block in groups.blocks
                         if block in model.block_seconds)
    largest = max(groups.seconds, default=0.0)
    print(f"{len(groups.blocks)} tile blocks of up to {2 ** TILE_BLOCK_BITS} "
          f"tiles, {measured_count} of them costed from their measured "
          f"profile seconds and bytes and the rest from the profiles' "
          f"declared figures; the costliest is {largest / 60:.1f}m, which no "
          f"worker's share can go below", file=sys.stderr)

    worker_count, blocks = _size_run(args, entries, gaps, model, setup_seconds,
                                     groups)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom,
                          args.max_zoom, header["tile_data_offset"],
                          attribution)

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)
    print(f"largest block holds {max(len(block) for block in blocks)} "
          f"records", file=sys.stderr)
    print(_tiles_line(blocks), file=sys.stderr)
    loads = block_loads(blocks, model, setup_seconds)
    _print_worker_predictions(loads, args.limits)
    load = worst_of(loads)
    broken = breaches(load, args.limits)
    print(f"worst worker: {load.seconds / 60:.0f}m predicted, {load.tiles} "
          f"output tiles, largest batch {load.batch_bytes / 2 ** 30:.2f} GiB",
          file=sys.stderr)
    if broken:
        print(f"::warning title=worker budget::the worst worker is over budget "
              f"on {', '.join(broken)} at {worker_count} workers",
              file=sys.stderr)
    worker_seconds = [worker_load.seconds for worker_load in loads]
    even_minutes = sum(worker_seconds) / len(blocks) / 60
    print(f"cost model predicts {sum(worker_seconds) / 3600:.1f} core-hours "
          f"including {setup_seconds:.0f}s setup per worker, slowest worker "
          f"{max(worker_seconds) / 60:.0f}m against an even "
          f"{even_minutes:.0f}m", file=sys.stderr)
    # stdout carries the attribution alone, for _pipeline.yml to refuse an
    # unattributed layer by; the parts carry it from source.json.
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
    print(f"::group::predicted per worker ({len(loads)} manifests)",
          file=sys.stderr)
    for worker_index, load in enumerate(loads):
        budget_share = load.seconds * limits.tail_factor / limits.job_seconds
        print(f"worker-{worker_index:03d}: {load.seconds / 60:6.1f}m "
              f"predicted, "
              f"{load.tiles} output tiles, {load.records} records, "
              f"{budget_share:.0%} of budget", file=sys.stderr)
    print("::endgroup::", file=sys.stderr)


def _settle_costs(args, resolved_source):
    """Work out what this run will be charged, and keep it here.

    Everything is settled together because it is one decision: the five
    per-axis seconds, what each tile block costs the profiles, what one
    worker's process pool buys, and what a worker costs before it starts.
    Where the state branch has recorded runs, each figure is the median of
    them, which one odd run cannot move far on its own. Where it has none, the
    reviewed constants stand, and a block nothing measured is charged the
    profiles' own declared estimates.

    A gap tile's weight is settled by measurement either way, by asking each
    profile once before any network work happens.

    The result is one CostModel, and every later step takes that one object:
    partitioning and sizing used to be handed the axes and the profile costs
    separately, and partitioning quietly went without the second.

    Args:
        args: The parsed command line, read for its profiles, its state
            document, its block state and any `--axis-seconds` override.
        resolved_source: The archive this run will read.

    Returns:
        The CostModel this run is priced by, and the per-worker setup seconds.
    """
    document = args.axis_state_document
    source_key = axis_key_for(resolved_source.url, resolved_source.schema.name)
    notes = []
    axis, setup_seconds = args.axis_seconds, WORKER_SETUP_SECONDS
    parallelism = None
    if document is not None:
        axis, shared = axis_state.axis_seconds(document, source_key, notes)
        setup_seconds = shared.worker_setup_seconds
        parallelism = shared.transform_parallelism
        print(f"axis state: {source_key} costed from "
              f"{axis_state.history_depth(document)} recorded run(s) of "
              f"{axis_state.HISTORY_LENGTH}, each coefficient the median of "
              f"its own", file=sys.stderr)
    profile_costs = _declared_profile_costs(args.profiles,
                                            resolved_source.schema)
    for note in notes:
        print(f"::warning title=axis state::{note}", file=sys.stderr)
    blocks = block_state.NO_BLOCK_COSTS
    if args.block_state and args.profiles:
        profiles_key = profile_combo_key(profile.name
                                         for profile in args.profiles)
        blocks = block_state.read_block_costs(args.block_state, source_key,
                                              profiles_key)
        print(f"block state: {len(blocks.seconds)} tile blocks' seconds and "
              f"{len(blocks.written_bytes)} blocks' written bytes measured for "
              f"{source_key}/{profiles_key}, each the median of up to "
              f"{axis_state.HISTORY_LENGTH} runs", file=sys.stderr)
    model = cost_model(axis=axis, profiles=profile_costs,
                       transform_parallelism=parallelism,
                       block_seconds=blocks.seconds,
                       block_bytes=blocks.written_bytes)
    print(f"costing: one worker's pool buys "
          f"{model.transform_parallelism:.2f}s of decode and profile work per "
          f"second of its wall clock", file=sys.stderr)
    return model, setup_seconds


def _declared_profile_costs(profiles, schema):
    """What each profile says it costs, for every tile block not yet measured.

    Only a gap tile's weight is measured here, by asking the profile once --
    there is nothing to estimate. Everything else is the profile's own
    declaration: what a profile measurably costs is kept per tile block, for
    one archive and one profile set, and a block that has it is charged that
    instead (see docs/ARCHITECTURE.md "Measured tile blocks").

    Args:
        profiles: The profile instances the run will build, or None.
        schema: The schema the output is written against, for the gap
            question.

    Returns:
        One ProfileCost per profile, in the same order.
    """
    costs = []
    for profile in profiles or []:
        costs.append(ProfileCost(
            name=profile.name,
            seconds_per_tile=profile.seconds_per_tile,
            bytes_per_output_tile=profile.bytes_per_output_tile,
            gap_bytes=profile.gap_bytes(schema),
            written_share=profile.written_share))
        print(f"costing: profile {profile.name}: "
              f"{profile.seconds_per_tile:.3g}s and "
              f"{profile.bytes_per_output_tile:.0f}B per real tile, written on "
              f"{profile.written_share:.1%} of the tiles it is handed "
              f"(declared, for unmeasured blocks), "
              f"{costs[-1].gap_bytes:.0f}B per gap tile (measured)",
              file=sys.stderr)
    return costs


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
    worker_count, blocks, unused_load, attempts = choose_worker_count(
        entries, gaps, model, args.limits, setup_seconds=setup_seconds,
        groups=groups)
    for tried, load, broken in attempts:
        verdict = f"{', '.join(broken)} over budget" if broken else "fits"
        print(f"sizing: {tried} workers, worst worker "
              f"{load.seconds / 60:.0f}m predicted, {load.tiles} output tiles "
              f"-- {verdict}", file=sys.stderr)
    return worker_count, blocks
