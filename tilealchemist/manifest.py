"""Everything `prepare_shards.py` hands the `build_shard.py` workers."""
import json
import os
import struct
from collections import namedtuple
from dataclasses import dataclass

from tilealchemist.schemas import SchemaName
from tilealchemist.zoom import ZoomLevel

RECORD = struct.Struct("<QQII")  # tile_id, offset, length, run_length; no framing needed.

Entry = namedtuple("Entry", ["tile_id", "offset", "length", "run_length"])


def write_manifest(path, entries):
    """Write entries out as fixed-width records.

    Args:
        path: File to write.
        entries: The entries to pack, in the order a worker will walk them.
    """
    with open(path, "wb") as file:
        for entry in entries:
            file.write(RECORD.pack(entry.tile_id, entry.offset, entry.length, entry.run_length))


def read_manifest(path):
    """Read a manifest back.

    Args:
        path: File to read.

    Returns:
        The entries it holds, in file order.
    """
    with open(path, "rb") as file:
        data = file.read()
    return [Entry(*fields) for fields in RECORD.iter_unpack(data)]


def write_worker_manifests(out_dir, blocks):
    """Write one manifest per worker.

    Args:
        out_dir: Directory the `worker-NNN.bin` files go in.
        blocks: One entry block per worker, in worker order. An empty block
            still gets its file, so worker N always has one to read.
    """
    for worker_index, block in enumerate(blocks):
        write_manifest(os.path.join(out_dir, f"worker-{worker_index:03d}.bin"), block)


@dataclass(frozen=True)
class SourceMetadata:
    """What every worker needs to know about the archive it reads.

    Travels as `source.json`, beside the per-worker manifests.

    Attributes:
        url: Absolute URL of the source archive.
        build: Human-readable build label, for logs.
        schema: Which schema its tiles are encoded in.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.
        tile_data_offset: Start of the archive's tile data, which the offsets
            in a manifest record are relative to.
    """

    url: str
    build: str
    schema: SchemaName
    min_zoom: ZoomLevel
    max_zoom: ZoomLevel
    tile_data_offset: int

    def as_json(self):
        """Plain strings and numbers, exactly what the matching CLI flags take."""
        return {
            "url": self.url,
            "build": self.build,
            "schema": self.schema.value,
            "min_zoom": self.min_zoom.value,
            "max_zoom": self.max_zoom.value,
            "tile_data_offset": self.tile_data_offset,
        }

    @classmethod
    def from_json(cls, document):
        """Back to members, so a hand-edited source.json fails here and not inside a worker."""
        return cls(
            url=document["url"],
            build=document["build"],
            schema=SchemaName(document["schema"]),
            min_zoom=ZoomLevel(document["min_zoom"]),
            max_zoom=ZoomLevel(document["max_zoom"]),
            tile_data_offset=document["tile_data_offset"],
        )


def write_source_metadata(out_dir, resolved_source, min_zoom, max_zoom, tile_data_offset):
    """Write the `source.json` the workers read.

    Args:
        out_dir: Directory the file goes in.
        resolved_source: The archive this run settled on.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.
        tile_data_offset: Start of the archive's tile data.
    """
    metadata = SourceMetadata(
        url=resolved_source.url,
        build=resolved_source.build,
        schema=resolved_source.schema.name,
        min_zoom=ZoomLevel(min_zoom),
        max_zoom=ZoomLevel(max_zoom),
        tile_data_offset=tile_data_offset,
    )
    with open(os.path.join(out_dir, "source.json"), "w") as source_file:
        json.dump(metadata.as_json(), source_file)


def read_source_metadata(path):
    """Read a worker's `source.json` back.

    Args:
        path: The file to read.

    Returns:
        Its contents as a SourceMetadata.

    Raises:
        KeyError: If the document is missing a key.
        ValueError: If its schema name or a zoom level is not one this build
            knows.
    """
    with open(path) as source_file:
        return SourceMetadata.from_json(json.load(source_file))
