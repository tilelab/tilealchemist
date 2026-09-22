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
from tilealchemist.usage import (
    PhaseSeconds,
    child_peak_rss_bytes,
    free_disk_bytes,
    peak_rss_bytes,
    report,
)


def split_manifest_entries(entries):
    # compute_gaps() tags a gap length=0, there being nothing to fetch for it.
    real_entries = [entry for entry in entries if entry.length > 0]
    gap_entries = [entry for entry in entries if entry.length == 0]
    return real_entries, gap_entries


def run_worker(args):
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
    totals = {"real_entries": len(real_entries), "gap_entries": len(gap_entries),
              "fetched_bytes": 0, "peak_batch_bytes": 0}

    if not real_entries and not gap_entries:
        close_shards(writers)
        print(f"done: (empty shard) -> {', '.join(args.out)}", file=sys.stderr)
        _report_worker_usage(args, profiles, counts, phases, wall_start, totals)
        return

    if real_entries:
        totals["fetched_bytes"], totals["peak_batch_bytes"] = _process_real_entries(
            real_entries, args, source, schema, profiles, writers, counts, phases)
    if gap_entries:
        with phases.phase("write"):
            _process_gap_entries(gap_entries, schema, profiles, writers, counts)

    with phases.phase("close"):
        close_shards(writers)

    for profile, out, profile_counts in zip(profiles, args.out, counts):
        print(f"done: profile={profile.name} written={profile_counts.written} "
              f"skipped={profile_counts.skipped} -> {out}", file=sys.stderr)
    _report_worker_usage(args, profiles, counts, phases, wall_start, totals)


def _report_worker_usage(args, profiles, counts, phases, wall_start, totals):
    for profile, out, profile_counts in zip(profiles, args.out, counts):
        report("profile", worker=args.worker_index, profile=profile.name,
               written=profile_counts.written, skipped=profile_counts.skipped,
               blobs=profile_counts.blobs,
               shard_bytes=os.path.getsize(out) if os.path.exists(out) else 0)
    wall_seconds = time.perf_counter() - wall_start
    report("worker", worker=args.worker_index, wall_seconds=wall_seconds,
           setup_seconds=wall_seconds - phases.total(),
           peak_rss=peak_rss_bytes(), child_peak_rss=child_peak_rss_bytes(),
           free_disk=free_disk_bytes(os.path.dirname(os.path.abspath(args.out[0]))),
           **totals, **phases.fields())


def _process_real_entries(real_entries, args, source, schema, profiles, writers, counts,
                          phases):
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
        free_disk_bytes(out_dir)
        with phases.phase("fetch"):
            blob = fetch_batch_blob(session, batch, batch_label, args.worker_index, source,
                                     args.download_report_interval)
        fetched_bytes += len(blob)
        peak_batch = max(peak_batch, len(blob))
        with phases.phase("transform"):
            for chunk_results in run_transform(blob, batch, source.min_zoom,
                                                source.max_zoom, profiles, schema, args):
                with phases.phase("write"):
                    for profile_counts, profile_runs, writer in zip(
                            counts, chunk_results, writers):
                        profile_counts.add(*writer.write(profile_runs))
                        writer.connection.commit()
        # A worker cannot afford two batches' bytes at once; drop before the next fetch.
        del blob
    return fetched_bytes, peak_batch


def _process_gap_entries(gap_entries, schema, profiles, writers, counts):
    """No fetch: one transform_gap() per profile covers every gap tile in the run."""
    for profile_counts, profile, writer in zip(counts, profiles, writers):
        gap_data = profile.transform_gap(schema)
        profile_counts.add(*write_gap_tiles(gap_entries, writer, gap_data))
