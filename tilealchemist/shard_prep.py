"""The run's one-time planning step; see docs/ARCHITECTURE.md "Fetching"."""
import os
import sys

from tilealchemist.attribution import compose_attribution, fetch_declared_attribution
from tilealchemist.cost import WORKER_SETUP_SECONDS, cost_weights
from tilealchemist.manifest import write_source_metadata, write_worker_manifests
from tilealchemist.partition import (
    block_gap_tiles,
    block_tiles,
    compute_gaps,
    partition_into_worker_blocks,
)
from tilealchemist.sizing import breaches, choose_worker_count, worst_load
from tilealchemist.pmtiles_index import collect_entries
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

    worker_count, blocks = _size_run(args, entries, gaps)
    write_worker_manifests(args.out_dir, blocks)
    write_source_metadata(args.out_dir, resolved_source, args.min_zoom, args.max_zoom,
                          header["tile_data_offset"])

    non_empty_count = sum(1 for block in blocks if block)
    print(f"wrote {len(blocks)} manifests to {args.out_dir} "
          f"({non_empty_count} non-empty)", file=sys.stderr)
    print(f"largest block holds {max(len(block) for block in blocks)} records", file=sys.stderr)
    print(_tiles_line(blocks, args.limits.max_tiles), file=sys.stderr)
    overruns = _cap_overruns(blocks, args.limits.max_tiles)
    if overruns:
        print(f"::warning title=worker budget::{len(overruns)} of {len(blocks)} blocks write "
              f"more tiles than --max-tiles, worst "
              f"{max(overruns, key=lambda run: run[2])[2]}; there is nowhere else to put the "
              f"work at {worker_count} workers, so raise --worker-count or raise --max-tiles",
              file=sys.stderr)
    load = worst_load(blocks, args.axis_seconds, args.profiles)
    broken = breaches(load, args.limits)
    print(f"worst worker: {load.seconds / 60:.0f}m predicted, {load.tiles} output tiles, "
          f"largest batch {load.batch_bytes / 2 ** 30:.2f} GiB", file=sys.stderr)
    if broken:
        print(f"::warning title=worker budget::the worst worker is over budget on "
              f"{', '.join(broken)} at {worker_count} workers", file=sys.stderr)
    worker_seconds = [WORKER_SETUP_SECONDS + cost_weights(block, args.axis_seconds,
                                                           args.profiles)[1]
                       for block in blocks]
    even_minutes = sum(worker_seconds) / len(blocks) / 60
    print(f"cost model predicts {sum(worker_seconds) / 3600:.1f} core-hours including "
          f"{WORKER_SETUP_SECONDS:.0f}s setup per worker, slowest worker "
          f"{max(worker_seconds) / 60:.0f}m against an even {even_minutes:.0f}m",
          file=sys.stderr)
    # stdout carries the attribution alone, for _pipeline.yml to hand to tile-join.
    print(attribution)


def _cap_overruns(blocks, max_tiles):
    """Blocks the cap could not hold; the last block takes the remainder however big it is.

    Args:
        blocks: One entry block per worker.
        max_tiles: The output tiles one worker may write, or 0 for no cap.

    Returns:
        `(index, records, tiles)` for every block over the cap.
    """
    if not max_tiles:
        return []
    return [(index, len(block), block_tiles(block)) for index, block in enumerate(blocks)
            if block_tiles(block) > max_tiles]


def _tiles_line(blocks, max_tiles):
    """Say what the worst block writes, and how much of the tile cap that takes.

    Args:
        blocks: One entry block per worker.
        max_tiles: The output tiles one worker may write, or 0 for no cap.

    Returns:
        That line, ready for stderr.
    """
    worst = max(blocks, key=block_tiles)
    tiles, gap_tiles = block_tiles(worst), block_gap_tiles(worst)
    against = (f"{100 * tiles / max_tiles:.0f}% of the {max_tiles} --max-tiles cap"
               if max_tiles else "against no --max-tiles cap")
    return (f"worst block: {len(worst)} records writing {tiles} output tiles "
            f"({gap_tiles} of them gap tiles), {against}")


def _size_run(args, entries, gaps):
    """The worker count this run uses and its blocks: as asked for, or the smallest that fits."""
    if args.worker_count != "auto":
        return args.worker_count, partition_into_worker_blocks(
            entries, gaps, args.worker_count, args.limits.max_tiles, args.axis_seconds)
    worker_count, blocks, _load, attempts = choose_worker_count(
        entries, gaps, args.axis_seconds, args.limits, profiles=args.profiles)
    for tried, load, broken in attempts:
        verdict = f"{', '.join(broken)} over budget" if broken else "fits"
        print(f"sizing: {tried} workers, worst worker {load.seconds / 60:.0f}m predicted, "
              f"{load.tiles} output tiles -- {verdict}", file=sys.stderr)
    return worker_count, blocks
