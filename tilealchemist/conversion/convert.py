"""`tilealchemist convert`: every profile over a PMTiles file on this machine.

Converts the tiles the archive holds, and nothing for tile ids it lacks. See
docs/explanation/architecture.md.
"""
import os
import sys
from collections.abc import Sequence
from typing import Any

from tilealchemist.archive.attribution import (
    compose_attribution,
    declared_attribution,
)
from tilealchemist.archive.directory import DirectoryEntry
from tilealchemist.archive.layer_writer import LayerWriter
from tilealchemist.archive.local import LocalArchive, open_archive
from tilealchemist.archive.schema_check import check_schema
from tilealchemist.archive.zoom import ZoomLevel
from tilealchemist.config import plugins
from tilealchemist.config.run_config import ConvertConfig, layer_names
from tilealchemist.conversion.partition import count_decoding_entries
from tilealchemist.conversion.transform_chunk import (
    ChunkResults,
    TileRun,
    TransformSettings,
)
from tilealchemist.conversion.transform_pool import run_transform
from tilealchemist.profiles.base import Profile
from tilealchemist.profiles.loader import load_profile
from tilealchemist.schemas.base import TileSchema


def convert(*, config: ConvertConfig, input_path: str, output_dir: str,
            processes: int | None, overwrite: bool) -> None:
    """Builds each layer of `config` from the archive at `input_path`.

    Writes `<output_dir>/<layer>.pmtiles` per layer; a layer without tiles
    gets no file.

    Args:
        config: The checked config.
        input_path: The PMTiles file to read.
        output_dir: Where the layers go.
        processes: Transform processes, or None for one per core.
        overwrite: Whether a layer may replace an existing file.

    Raises:
        ValueError: If a profile lacks a dependency or `PROFILE`, two
            profiles share a name, a layer file exists, the archive isn't
            gzipped MVT, isn't in the schema or has none of the zoom
            range, or nothing would be credited.
    """
    profiles: list[Profile] = [load_profile(path)()
                               for path in config.profiles]
    names: list[str] = layer_names(config.profiles, profiles)
    outputs: list[str] = [os.path.join(output_dir, f"{name}.pmtiles")
                          for name in names]
    existing: list[str] = [path for path in outputs if os.path.exists(path)]
    if existing and not overwrite:
        raise ValueError(f"{', '.join(existing)} already "
                         f"{'exists' if len(existing) == 1 else 'exist'}; "
                         f"pass --overwrite to replace, or -o for another "
                         f"directory")
    schema: TileSchema = plugins.schema(config.schema)
    os.makedirs(output_dir, exist_ok=True)

    archive: LocalArchive
    with open_archive(input_path) as archive:
        min_zoom: ZoomLevel = max(config.min_zoom,
                                  ZoomLevel(archive.header["min_zoom"]))
        max_zoom: ZoomLevel = min(config.max_zoom,
                                  ZoomLevel(archive.header["max_zoom"]))
        if min_zoom > max_zoom:
            raise ValueError(
                f"{input_path} holds zoom levels {archive.header['min_zoom']} "
                f"to {archive.header['max_zoom']}, none of {config.min_zoom} "
                f"to {config.max_zoom}")
        metadata: Any = archive.metadata()
        # Checks that the metadata is a JSON object, before check_schema().
        attribution: str = compose_attribution(
            declared_attribution(metadata), config.attribution)
        check_schema(schema, metadata, input_path)
        print(f"input={input_path} (schema {schema.name}, zoom {min_zoom} to "
              f"{max_zoom}), profiles="
              f"{', '.join(profile.name for profile in profiles)}",
              file=sys.stderr)
        entries: list[DirectoryEntry] = archive.entries(min_zoom, max_zoom)
        print(f"{len(entries)} entries, {count_decoding_entries(entries)} "
              f"blobs", file=sys.stderr)
        writers: list[LayerWriter] = [
            LayerWriter(output,
                        _layer_metadata(profile, schema, min_zoom, max_zoom,
                                        attribution),
                        archive.header)
            for output, profile in zip(outputs, profiles)]
        _transform(archive, entries,
                   TransformSettings(min_zoom, max_zoom, profiles, schema),
                   list(config.profiles), processes or os.cpu_count() or 1,
                   writers)

    writer: LayerWriter
    for writer in writers:
        if writer.finalize():
            print(f"layer: {writer.path}", file=sys.stderr)
            continue
        print(f"no tiles: {writer.path} left out", file=sys.stderr)
        # Replaced by nothing, under --overwrite.
        if os.path.exists(writer.path):
            os.remove(writer.path)


def _layer_metadata(profile: Profile, schema: TileSchema,
                    min_zoom: ZoomLevel, max_zoom: ZoomLevel,
                    attribution: str) -> dict[str, Any]:
    """Returns the PMTiles metadata of `profile`'s layer."""
    return {
        "name": profile.name,
        "format": "pbf",
        "minzoom": str(min_zoom),
        "maxzoom": str(max_zoom),
        "attribution": attribution,
        "vector_layers": profile.vector_layers(schema),
    }


def _transform(archive: LocalArchive, entries: Sequence[DirectoryEntry],
               settings: TransformSettings, profile_paths: list[str],
               processes: int, writers: Sequence[LayerWriter]) -> None:
    """Runs every profile over the entries, writing each chunk away."""
    chunk_results: ChunkResults
    profile_runs: list[TileRun]
    writer: LayerWriter
    for chunk_results in run_transform(
            archive.data, -archive.header["tile_data_offset"], entries,
            settings, profile_paths, processes):
        for profile_runs, writer in zip(chunk_results, writers):
            writer.write(profile_runs)
