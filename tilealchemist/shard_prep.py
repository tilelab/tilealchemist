"""The run's one-time planning step; see docs/ARCHITECTURE.md "Fetching"."""
import os
import sys

from tilealchemist import axis_state
from tilealchemist.attribution import compose_attribution, fetch_declared_attribution
from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.manifest import axis_key_for, write_source_metadata, write_worker_manifests
from tilealchemist.partition import count_gap_tiles, count_output_tiles
from tilealchemist.sizing import breaches, choose_worker_count, worst_load
from tilealchemist.pmtiles_index import collect_entries, compute_gaps
from tilealchemist.ranged_fetch import make_session
from tilealchemist.sources import resolve_source


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

    axis, profile_costs, setup_seconds = _settle_costs(args, resolved_source)

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

    worker_count, blocks = _size_run(args, entries, gaps, axis, profile_costs, setup_seconds)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom, args.max_zoom,
                          header["tile_data_offset"])

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)
    print(f"largest block holds {max(len(block) for block in blocks)} records", file=sys.stderr)
    print(_tiles_line(blocks), file=sys.stderr)
    load = worst_load(blocks, axis, profile_costs, setup_seconds)
    broken = breaches(load, args.limits)
    print(f"worst worker: {load.seconds / 60:.0f}m predicted, {load.tiles} output tiles, "
          f"largest batch {load.batch_bytes / 2 ** 30:.2f} GiB", file=sys.stderr)
    if broken:
        print(f"::warning title=worker budget::the worst worker is over budget on "
              f"{', '.join(broken)} at {worker_count} workers", file=sys.stderr)
    worker_seconds = [setup_seconds + cost_weights(block, axis, profile_costs)[1]
                       for block in blocks]
    even_minutes = sum(worker_seconds) / len(blocks) / 60
    print(f"cost model predicts {sum(worker_seconds) / 3600:.1f} core-hours including "
          f"{setup_seconds:.0f}s setup per worker, slowest worker "
          f"{max(worker_seconds) / 60:.0f}m against an even {even_minutes:.0f}m",
          file=sys.stderr)
    # stdout carries the attribution alone, for _pipeline.yml to hand to tile-join.
    print(attribution)


def _settle_costs(args, resolved_source):
    """Work out what this run will be charged, and keep it here.

    Three things are settled together because they are one decision: the five
    per-axis seconds, what each profile costs, and what a worker costs before
    it starts. Where the state branch has recorded runs, each figure is the
    median of them, clamped towards the reviewed one so a single odd run moves
    the model without taking it over. Where it has none, the reviewed constants
    and the profiles' own declared estimates stand.

    A gap tile's weight is settled by measurement either way, by asking each
    profile once before any network work happens.

    Args:
        args: The parsed command line, read for its profiles, its state
            document and any `--axis-seconds` override.
        resolved_source: The archive this run will read.

    Returns:
        The per-axis seconds, one ProfileCost per profile, and the per-worker
        setup seconds.
    """
    document = args.axis_state_document
    notes = []
    axis, setup_seconds = args.axis_seconds, WORKER_SETUP_SECONDS
    if document is not None:
        source_key = axis_key_for(resolved_source.url, resolved_source.schema.name)
        axis, shared = axis_state.axis_seconds(document, source_key, notes)
        setup_seconds = shared.worker_setup_seconds
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
    return axis, profile_costs, setup_seconds


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


def _size_run(args, entries, gaps, axis, profile_costs, setup_seconds):
    """The worker count this run sized itself to, and its blocks.

    Args:
        args: The parsed command line, for the limits.
        entries: The archive's directory entries for this run.
        gaps: The gap records covering what the archive does not hold.
        axis: The per-axis seconds to charge.
        profile_costs: What each profile costs, as settled for this run.
        setup_seconds: What a worker costs before it reaches its first record.

    Returns:
        The chosen worker count and its blocks. Every count tried is logged,
        so the log says which limit pushed the run to the count it landed on.
    """
    worker_count, blocks, _load, attempts = choose_worker_count(
        entries, gaps, axis, args.limits, profile_costs=profile_costs,
        setup_seconds=setup_seconds)
    for tried, load, broken in attempts:
        verdict = f"{', '.join(broken)} over budget" if broken else "fits"
        print(f"sizing: {tried} workers, worst worker {load.seconds / 60:.0f}m predicted, "
              f"{load.tiles} output tiles -- {verdict}", file=sys.stderr)
    return worker_count, blocks
