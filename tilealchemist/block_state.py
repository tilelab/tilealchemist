"""The measured seconds per tile block a run keeps for the next one; see docs/ARCHITECTURE.md "Measured tile blocks"."""
import json
import os
import statistics
from collections import namedtuple

from tilealchemist.axis_state import HISTORY_LENGTH
from tilealchemist.tile_blocks import parse_block_seconds

VERSION = 1

# The state branch keeps one file per archive and profile set under this directory.
DEFAULT_BLOCK_STATE_DIR = "state/blocks"

BlockMeasurement = namedtuple("BlockMeasurement", "source_key profiles seconds workers")


def block_state_path(root, source_key, profiles):
    """Where one archive and profile set's block history lives.

    Args:
        root: The directory every block file sits under.
        source_key: The archive's key, as `SourceMetadata.axis_key` gives it.
        profiles: The profile set's key, as `profile_combo_key()` gives it.

    Returns:
        The file's path, `/`-separated so it serves both a checkout and the
        contents API.
    """
    return f"{root.rstrip('/')}/{source_key}/{profiles}.json"


def measure_blocks(rows):
    """Sum every worker's per-block seconds into the run's.

    A block can be split across workers -- a run partitioned before blocks
    were the unit, or one block bigger than a worker's share -- so the run's
    figure for it is the sum of what each worker spent there.

    Args:
        rows: Parsed usage rows for the whole run.

    Returns:
        The run's BlockMeasurement, or None where no worker reported blocks.

    Raises:
        ValueError: If the rows name more than one archive or profile set,
            which means two runs' files were mixed up.
    """
    block_rows = [row for row in rows if row.get("scope") == "blocks"]
    if not block_rows:
        return None
    keys = {(row.get("source_key"), row.get("profiles")) for row in block_rows}
    if len(keys) != 1:
        raise ValueError(f"block rows name {len(keys)} archive/profile pairs: {sorted(keys)}")
    (source_key, profiles), = keys
    seconds = {}
    for row in block_rows:
        for block, value in parse_block_seconds(row.get("seconds")).items():
            seconds[block] = seconds.get(block, 0.0) + value
    return BlockMeasurement(source_key=source_key, profiles=profiles, seconds=seconds,
                            workers=len(block_rows))


def empty_document(source_key, profiles):
    """A block document holding no measurement at all.

    Args:
        source_key: The archive the document is for.
        profiles: The profile set it is for.

    Returns:
        The document.
    """
    return {"version": VERSION, "source_key": source_key, "profiles": profiles, "blocks": {}}


def record_blocks(document, measurement, build=None):
    """Append one run's per-block seconds to the document's history.

    Every block keeps its own last `HISTORY_LENGTH` observations, the same
    depth the axis coefficients keep. A block this run did not cover keeps the
    history it had: a z0..z11 run says nothing about z14.

    Args:
        document: The document to extend, mutated in place.
        measurement: The run's BlockMeasurement.
        build: The archive build the run read, kept for a reader.

    Returns:
        A line saying what was recorded, for the job log.
    """
    blocks = document.setdefault("blocks", {})
    for block, seconds in measurement.seconds.items():
        history = blocks.get(str(block), [])
        # Milliseconds: what a block costs is never balanced finer than that.
        blocks[str(block)] = (history + [round(seconds, 3)])[-HISTORY_LENGTH:]
    if build:
        document["build"] = build
    return (f"blocks {measurement.source_key}/{measurement.profiles}: recorded "
            f"{len(measurement.seconds)} blocks from {measurement.workers} workers, "
            f"{len(blocks)} on file")


def block_medians(document):
    """What the recorded runs say each block costs.

    Args:
        document: The block document.

    Returns:
        A mapping of block key to its median observed seconds, skipping
        anything in the file that is not a list of non-negative numbers.
    """
    medians = {}
    for block, history in document.get("blocks", {}).items():
        values = [float(value) for value in history if isinstance(value, (int, float))
                  and value >= 0] if isinstance(history, list) else []
        if values:
            medians[int(block)] = statistics.median(values)
    return medians


def read_block_medians(root, source_key, profiles):
    """Read the medians for one archive and profile set out of a checkout.

    Args:
        root: The checked-out block state directory, or None.
        source_key: The archive the run reads.
        profiles: The profile set the run builds.

    Returns:
        The medians, empty where there is no directory or no file for this
        pair -- the first run of anything has nothing measured yet.

    Raises:
        ValueError: If the file exists but is not a JSON object.
    """
    if not root:
        return {}
    path = block_state_path(root, source_key, profiles)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{path} holds {type(document).__name__}, not a JSON object")
    return block_medians(document)
