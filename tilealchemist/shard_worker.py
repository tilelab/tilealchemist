"""One worker's shard build; see docs/ARCHITECTURE.md "Parallelism"."""
import os
import sys
import time

from tilealchemist.fetch_batching import fetch_batch_blob, plan_fetch_batches
from tilealchemist.manifest import read_manifest, read_source_metadata
from tilealchemist.mbtiles import (
    ProfileTileCounts,
    close_shards,
    init_mbtiles,
    write_gap_tiles,
)
from tilealchemist.ranged_fetch import make_session
from tilealchemist.schemas import SCHEMAS
from tilealchemist.transform_pool import run_transform
from tilealchemist.usage import PhaseSeconds, TransformUsage, report


def split_manifest_entries(entries):
    """Split a manifest into the entries to fetch and the gaps to fill.

    Args:
        entries: This worker's manifest records.

    Returns:
        The real entries and the gap entries, told apart by the `length=0`
        that compute_gaps() marks a gap with, there being nothing to fetch
        for one.
    """
    real_entries = [entry for entry in entries if entry.length > 0]
    gap_entries = [entry for entry in entries if entry.length == 0]
    return real_entries, gap_entries


def run_worker(args):
    """Build one worker's shard of every profile's output layer.

    One fetch per batch, shared between the profiles, then a transform and a
    write per chunk. The shards are marked complete on the way out, so that a
    worker cut short leaves shards a later step can tell are unfinished.

    Args:
        args: The parsed command line from build_shard.py.
    """
    wall_start = time.perf_counter()
    phases = PhaseSeconds()
    source = read_source_metadata(args.source)
    # From the source, never a flag: no worker may disagree with the walk.
    schema = SCHEMAS[source.schema]
    profiles = [profile_class() for profile_class in args.profile_classes]
    print(f"source={source.url} (build {source.build}, schema {schema.name}), "
          f"profiles={', '.join(profile.name for profile in profiles)}", file=sys.stderr)

    entries = read_manifest(args.manifest)
    real_entries, gap_entries = split_manifest_entries(entries)
    print(f"{len(real_entries)} real entries + {len(gap_entries)} gap ranges assigned",
          file=sys.stderr)

    writers = [init_mbtiles(out, source.min_zoom, source.max_zoom, profile, schema,
                             args.shard_layout)
                for out, profile in zip(args.out, profiles)]
    counts = [ProfileTileCounts() for _ in profiles]
    gap_totals = [(0, 0) for _ in profiles]
    usage = TransformUsage(len(profiles))
    # The key travels with the measurement: the fit must never guess which archive it read.
    totals = {"source_key": source.axis_key,
              "real_entries": len(real_entries), "gap_entries": len(gap_entries),
              "fetched_bytes": 0, "peak_batch_bytes": 0}

    if not real_entries and not gap_entries:
        close_shards(writers)
        print(f"done: (empty shard) -> {', '.join(args.out)}", file=sys.stderr)
        _report_worker_usage(args, profiles, counts, phases, wall_start, totals, usage,
                              gap_totals)
        return

    if real_entries:
        totals["fetched_bytes"], totals["peak_batch_bytes"] = _process_real_entries(
            real_entries, args, source, schema, profiles, writers, counts, phases, usage)
    if gap_entries:
        with phases.phase("write"):
            gap_totals = _process_gap_entries(gap_entries, schema, profiles, writers, counts)

    with phases.phase("close"):
        close_shards(writers)

    for profile, out, profile_counts in zip(profiles, args.out, counts):
        print(f"done: profile={profile.name} written={profile_counts.written} "
              f"skipped={profile_counts.skipped} -> {out}", file=sys.stderr)
    _report_worker_usage(args, profiles, counts, phases, wall_start, totals, usage, gap_totals)


def _report_worker_usage(args, profiles, counts, phases, wall_start, totals, usage,
                          gap_totals):
    """Print this worker's usage lines: one per profile, and one for itself.

    Two scopes, split by what the measurement belongs to. A profile's own cost
    -- the seconds its `transform_tile()` took, and the bytes its output came to
    -- goes on its own line, where the fit can key it by profile. Everything
    that belongs to the archive or the runner instead goes on the worker's line.

    Args:
        args: The parsed command line.
        profiles: The profiles that ran, in output order.
        counts: Each profile's tile counts, in the same order.
        phases: The per-phase seconds.
        wall_start: When the worker started, by `time.perf_counter()`.
        totals: The worker-scoped fields to report alongside, including the
            archive key the measurements belong to.
        usage: What the transform cost, merged across every chunk this worker
            ran.
        gap_totals: Each profile's `(gap tiles, gap payload bytes)`, reported
            apart from the real tiles' so a fit can keep the two populations
            separate.
    """
    lines = []
    for profile, out, profile_counts, profile_seconds, output_bytes, (gap_tiles, gap_bytes) in zip(
            profiles, args.out, counts, usage.profile_seconds, usage.profile_output_bytes,
            gap_totals):
        lines.append(report("profile", worker=args.worker_index, profile=profile.name,
                            written=profile_counts.written, skipped=profile_counts.skipped,
                            blobs=profile_counts.blobs,
                            transform_seconds=profile_seconds,
                            output_bytes=output_bytes,
                            gap_tiles=gap_tiles, gap_bytes=gap_bytes,
                            shard_bytes=os.path.getsize(out) if os.path.exists(out) else 0))
    wall_seconds = time.perf_counter() - wall_start
    lines.append(report("worker", worker=args.worker_index, wall_seconds=wall_seconds,
                        setup_seconds=wall_seconds - phases.total(),
                        **totals, **usage.fields(), **phases.fields()))
    if args.usage_out:
        # A file, not just the log: the job that fits these has artifacts, not log scrape access.
        with open(args.usage_out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _process_real_entries(real_entries, args, source, schema, profiles, writers, counts,
                          phases, usage):
    """Fetch, transform and write every real entry, batch by batch.

    Each batch's bytes are dropped before the next is fetched: a worker's
    memory budget covers one batch, not the whole block's byte sum.

    Args:
        real_entries: The entries to fetch, in offset order.
        args: The parsed command line.
        source: The archive's metadata.
        schema: The schema its tiles are in.
        profiles: The profiles to run, in output order.
        writers: Each profile's shard, in the same order.
        counts: Each profile's tile counts, in the same order.
        phases: The per-phase seconds to charge the work to.
        usage: The worker's running transform measurements.

    Returns:
        The bytes fetched in total, and the largest single batch.
    """
    batches = plan_fetch_batches(real_entries, args.max_fetch_gap)
    if len(batches) > 1:
        print(f"{len(real_entries)} real entries fetched in {len(batches)} range requests "
              f"(gaps over {args.max_fetch_gap} bytes are not fetched through)",
              file=sys.stderr)
    session = make_session()
    fetched_bytes = peak_batch = 0
    out_dir = os.path.dirname(os.path.abspath(args.out[0]))
    for batch_index, batch in enumerate(batches, start=1):
        batch_label = f" {batch_index}/{len(batches)}" if len(batches) > 1 else ""
        # Unmapped and deleted on the way out, so two batches never overlap.
        with fetch_batch_blob(session, batch, batch_label, args.worker_index, source,
                               args.download_report_interval, out_dir, phases) as blob:
            fetched_bytes += len(blob)
            peak_batch = max(peak_batch, len(blob))
            with phases.phase("transform"):
                for chunk_results in run_transform(blob, batch, source.min_zoom,
                                                    source.max_zoom, profiles, schema, args,
                                                    usage):
                    with phases.phase("write"):
                        for profile_counts, profile_runs, writer in zip(
                                counts, chunk_results, writers):
                            profile_counts.add(*writer.write(profile_runs))
                            writer.connection.commit()
    return fetched_bytes, peak_batch


def _process_gap_entries(gap_entries, schema, profiles, writers, counts):
    """Fill every gap tile, without fetching anything.

    A gap carries no source data, so one `transform_gap()` per profile covers
    every gap tile in the run between them.

    Args:
        gap_entries: The gap records this worker carries.
        schema: The schema the output is written against.
        profiles: The profiles to run, in output order.
        writers: Each profile's shard, in the same order.
        counts: Each profile's tile counts, in the same order.

    Returns:
        Each profile's `(gap tiles, gap payload bytes)`, in the same order.
        Kept apart from the real tiles' totals because the two are different
        sizes and a run's mix of them swings far too wide to average.
    """
    gap_totals = []
    for profile_counts, profile, writer in zip(counts, profiles, writers):
        gap_data = profile.transform_gap(schema)
        written, skipped, blobs = write_gap_tiles(gap_entries, writer, gap_data)
        profile_counts.add(written, skipped, blobs)
        gap_totals.append((written, written * len(gap_data) if gap_data else 0))
    return gap_totals
