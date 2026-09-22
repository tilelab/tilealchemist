"""Chunking a batch and running it across processes; see docs/ARCHITECTURE.md "Parallelism"."""
import collections
import concurrent.futures
import operator
import sys

from tilealchemist.partition import partition_by_cost
from tilealchemist.profiles import load_profile
from tilealchemist.schemas import SCHEMAS
from tilealchemist.transform import TransformProgress, transform_batch_blob_multi
from tilealchemist.usage import TransformUsage, report_chunk

# Spare chunks per process, so one finishing early pulls the next instead of idling.
TRANSFORM_CHUNKS_PER_WORKER = 8

SUBMIT_LEAD = 2


def _chunk_entries(real_entries, transform_workers):
    if transform_workers <= 1 or len(real_entries) <= 1:
        return [real_entries]
    chunk_count = min(len(real_entries), transform_workers * TRANSFORM_CHUNKS_PER_WORKER)
    chunks = partition_by_cost(real_entries, chunk_count,
                               atomic_key=operator.attrgetter("offset"))
    return [chunk for chunk in chunks if chunk]


def _blob_slice_for_chunk(blob, batch_offset, chunk_entries):
    chunk_offset = chunk_entries[0].offset
    chunk_length = max(entry.offset + entry.length for entry in chunk_entries) - chunk_offset
    start = chunk_offset - batch_offset
    return blob[start:start + chunk_length], chunk_offset


# One picklable value; `args` cannot serve, carrying profile classes a worker cannot unpickle.
ChunkJob = collections.namedtuple(
    "ChunkJob", "profile_paths schema_name min_zoom max_zoom report_interval")


def _transform_chunk(job, blob_slice, blob_slice_offset, chunk_entries, chunk_index):
    profiles = [load_profile(path)() for path in job.profile_paths]
    batch = (blob_slice_offset, len(blob_slice), chunk_entries)
    progress = TransformProgress(len(chunk_entries), job.report_interval,
                                  label=f"transforming chunk {chunk_index + 1}")
    usage = TransformUsage()
    results = transform_batch_blob_multi(blob_slice, batch, job.min_zoom, job.max_zoom, progress,
                                          profiles, SCHEMAS[job.schema_name], usage)
    report_chunk(usage, chunk_index + 1, len(blob_slice))
    return results


def _pooled_chunk_results(blob, batch_offset, chunks, job, max_workers):
    # Every queued chunk's blob slice is held by the parent, so it must not queue them all.
    in_flight = max_workers + SUBMIT_LEAD
    waiting = iter(list(enumerate(chunks)))
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = {}

        def submit_next():
            for index, chunk in waiting:
                blob_slice, blob_slice_offset = _blob_slice_for_chunk(blob, batch_offset, chunk)
                future = executor.submit(_transform_chunk, job, blob_slice, blob_slice_offset,
                                          chunk, index)
                pending[future] = (index, len(chunk), len(blob_slice))
                return True
            return False

        while len(pending) < in_flight and submit_next():
            pass
        while pending:
            ready = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED).done
            future = min(ready, key=pending.__getitem__)
            ready = None
            # pop, not index: a live Future pins its chunk's output for the whole phase.
            index, entry_count, byte_count = pending.pop(future)
            chunk_results = future.result()
            future = None
            submit_next()
            yield index, entry_count, byte_count, chunk_results


def run_transform(blob, batch, min_zoom, max_zoom, profiles, schema, args):
    batch_offset, _batch_length, real_entries = batch
    chunks = _chunk_entries(real_entries, args.transform_workers)

    fanout = (f", {len(chunks)} chunks across up to {args.transform_workers} processes"
              if len(chunks) > 1 else "")
    print(f"starting transform for profiles "
          f"{', '.join(repr(profile.name) for profile in profiles)} "
          f"({len(real_entries)} entries{fanout})", file=sys.stderr)

    if len(chunks) <= 1:
        transform_progress = TransformProgress(len(real_entries), args.report_interval)
        usage = TransformUsage()
        results = transform_batch_blob_multi(blob, batch, min_zoom, max_zoom, transform_progress,
                                             profiles, schema, usage)
        report_chunk(usage, 1, len(blob))
        yield results
        return

    job = ChunkJob(args.profile, schema.name, min_zoom, max_zoom, args.report_interval)
    completed = _pooled_chunk_results(blob, batch_offset, chunks, job, args.transform_workers)
    for done, (index, entry_count, byte_count, chunk_results) in enumerate(completed, start=1):
        yield chunk_results
        print(f"chunk {index + 1} done ({done}/{len(chunks)} chunks, "
              f"{entry_count} entries, {byte_count} bytes)", file=sys.stderr)
