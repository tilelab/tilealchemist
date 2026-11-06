"""Cutting tile data into chunks and running them across processes."""
import concurrent.futures
import sys
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from typing import NamedTuple

from tilealchemist.archive.directory import DirectoryEntry
from tilealchemist.archive.zoom import ZoomLevel
from tilealchemist.config import plugins
from tilealchemist.conversion.partition import count_blob_bytes, share_bounds
from tilealchemist.conversion.transform_chunk import (
    ChunkResults,
    TileData,
    TransformSettings,
    transform_chunk,
)
from tilealchemist.profiles.loader import load_profile

# Chunks per process, so one finishing early takes the next instead of
# idling: equal bytes aren't equal cost (z9-z10 bytes cost about twice what
# z14 bytes do), and four chunks of one real `convert` finished minutes apart.
CHUNKS_PER_PROCESS: int = 8

# Chunks queued beyond one per process; each holds its bytes until done.
SUBMIT_LEAD: int = 2

# A chunk's bytes are copied into its process and its output held until
# written, so chunks stay below this unless one blob is larger.
MAX_CHUNK_BYTES: int = 64 * 2 ** 20


class ChunkJob(NamedTuple):
    """The picklable TransformSettings: profiles travel as their paths."""

    profile_paths: list[str]
    schema_name: str
    min_zoom: ZoomLevel
    max_zoom: ZoomLevel


def _chunk_entries(entries: Sequence[DirectoryEntry],
                   process_count: int) -> list[Sequence[DirectoryEntry]]:
    """Cuts entries into chunks of equal blob bytes.

    Several per process where there is a pool, and none over
    MAX_CHUNK_BYTES unless one blob is. A blob's entries stay in one chunk.
    """
    per_process: int = (process_count * CHUNKS_PER_PROCESS
                        if process_count > 1 else 1)
    by_size: int = -(-count_blob_bytes(entries) // MAX_CHUNK_BYTES)
    chunk_count: int = max(1, min(len(entries), max(per_process, by_size)))
    bounds: list[int] = share_bounds(entries, chunk_count)
    return [entries[start:stop] for start, stop in zip(bounds, bounds[1:])
            if start < stop]


def _chunk_span(chunk_entries: Sequence[DirectoryEntry]) -> tuple[int, int]:
    """Returns the tile data offset and length a chunk's blobs span.

    The span ends at the furthest entry's end, not the last's, so blobs may
    overlap.
    """
    chunk_offset: int = chunk_entries[0].offset
    chunk_end: int = max(entry.offset + entry.length
                         for entry in chunk_entries)
    return chunk_offset, chunk_end - chunk_offset


def _chunk_buffer(buffer: TileData, buffer_offset: int,
                  chunk_entries: Sequence[DirectoryEntry]
                  ) -> tuple[bytes, int]:
    """Returns a copy of a chunk's bytes and the offset they start at."""
    chunk_offset: int
    chunk_length: int
    chunk_offset, chunk_length = _chunk_span(chunk_entries)
    start: int = chunk_offset - buffer_offset
    return buffer[start:start + chunk_length], chunk_offset


def _run_chunk_in_process(job: ChunkJob, chunk_buffer: bytes,
                          chunk_offset: int,
                          chunk_entries: Sequence[DirectoryEntry],
                          chunk_index: int) -> ChunkResults:
    """Transforms one chunk inside a pool process.

    Profiles are loaded again by path and the schema by name, since neither
    pickles.
    """
    settings: TransformSettings = TransformSettings(
        job.min_zoom, job.max_zoom,
        [load_profile(path)() for path in job.profile_paths],
        plugins.schema(job.schema_name))
    return transform_chunk(chunk_buffer, chunk_offset, chunk_entries, settings,
                           label=f"transforming chunk {chunk_index + 1}")


def _inline_chunk_results(buffer: TileData, buffer_offset: int,
                          chunks: Sequence[Sequence[DirectoryEntry]],
                          settings: TransformSettings
                          ) -> Iterator[tuple[int, int, int, ChunkResults]]:
    """Runs chunks one after another in this process.

    Yields:
        `(chunk index, entry count, byte count, results)` per chunk.
    """
    index: int
    chunk: Sequence[DirectoryEntry]
    for index, chunk in enumerate(chunks):
        yield (index, len(chunk), _chunk_span(chunk)[1],
               transform_chunk(buffer, buffer_offset, chunk, settings,
                               label=f"transforming chunk {index + 1}"))


def _pooled_chunk_results(buffer: TileData, buffer_offset: int,
                          chunks: Sequence[Sequence[DirectoryEntry]],
                          job: ChunkJob, process_count: int
                          ) -> Iterator[tuple[int, int, int, ChunkResults]]:
    """Runs chunks across a process pool.

    At most `process_count + SUBMIT_LEAD` chunks are in flight: submitting
    copies a chunk's bytes into memory, so submitting all at once would hold
    all the tile data.

    Yields:
        `(chunk index, entry count, byte count, results)` per chunk, as
        they finish.
    """
    in_flight: int = process_count + SUBMIT_LEAD
    waiting: Iterator[tuple[int, Sequence[DirectoryEntry]]] = iter(
        list(enumerate(chunks)))
    executor: concurrent.futures.ProcessPoolExecutor
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=process_count) as executor:
        pending: dict[Future[ChunkResults], tuple[int, int, int]] = {}

        def top_up() -> None:
            """Submits chunks until `in_flight` are pending or none are left."""
            index: int
            chunk: Sequence[DirectoryEntry]
            for index, chunk in waiting:
                chunk_buffer: bytes
                chunk_offset: int
                chunk_buffer, chunk_offset = _chunk_buffer(
                    buffer, buffer_offset, chunk)
                submitted: Future[ChunkResults] = executor.submit(
                    _run_chunk_in_process, job, chunk_buffer, chunk_offset,
                    chunk, index)
                pending[submitted] = (index, len(chunk), len(chunk_buffer))
                if len(pending) >= in_flight:
                    return

        top_up()
        ready: set[Future[ChunkResults]]
        future: Future[ChunkResults]
        index: int
        entry_count: int
        byte_count: int
        while pending:
            ready = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED).done
            future = min(ready, key=pending.__getitem__)
            # A done Future holds its chunk's output, so keep no reference
            # but `chunk_results`. Keeping every Future once held a planet's
            # whole output and ran `convert` out of memory.
            del ready
            index, entry_count, byte_count = pending.pop(future)
            chunk_results: ChunkResults = future.result()
            del future
            # Before the yield, so the pool keeps working while the caller
            # writes.
            top_up()
            yield index, entry_count, byte_count, chunk_results


def run_transform(buffer: TileData, buffer_offset: int,
                  entries: Sequence[DirectoryEntry],
                  settings: TransformSettings, profile_paths: list[str],
                  processes: int) -> Iterator[ChunkResults]:
    """Transforms entries, in this process or across a pool.

    Args:
        buffer: The tile data the entries point into.
        buffer_offset: The tile data offset `buffer` starts at.
        entries: The entries, in offset order.
        settings: The run's settings.
        profile_paths: The profiles' files, in `settings.profiles` order, for
            pool processes to load.
        processes: The most processes the pool may run.

    Yields:
        Each chunk's results as it finishes, for the caller to write away.
    """
    chunks: list[Sequence[DirectoryEntry]] = _chunk_entries(entries,
                                                            processes)
    pooled: bool = processes > 1 and len(chunks) > 1
    fanout: str = (f" across up to {processes} processes" if pooled
                   else " in this process")
    print(f"starting transform for profiles "
          f"{', '.join(repr(profile.name) for profile in settings.profiles)} "
          f"({len(entries)} entries in {len(chunks)} chunks{fanout})",
          file=sys.stderr)

    completed: Iterator[tuple[int, int, int, ChunkResults]]
    if pooled:
        job: ChunkJob = ChunkJob(profile_paths, settings.schema.name,
                                 settings.min_zoom, settings.max_zoom)
        completed = _pooled_chunk_results(buffer, buffer_offset, chunks, job,
                                          processes)
    else:
        completed = _inline_chunk_results(buffer, buffer_offset, chunks,
                                          settings)
    done: int
    index: int
    entry_count: int
    byte_count: int
    chunk_results: ChunkResults
    for done, (index, entry_count, byte_count,
               chunk_results) in enumerate(completed, start=1):
        yield chunk_results
        print(f"chunk {index + 1} done ({done}/{len(chunks)} chunks, "
              f"{entry_count} entries, {byte_count} bytes)", file=sys.stderr)
