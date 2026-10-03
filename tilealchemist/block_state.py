"""The measured seconds and bytes per tile block a run keeps for the next one; see docs/ARCHITECTURE.md "Measured tile blocks"."""
import json
import os
import statistics
from collections import namedtuple

from tilealchemist.axis_state import HISTORY_LENGTH
from tilealchemist.tile_blocks import parse_block_values

# 2 keys a block by the blob's home rather than the tile's own block, and adds the written bytes.
VERSION = 2

# The state branch keeps one file per archive and profile set under this directory.
DEFAULT_BLOCK_STATE_DIR = "state/blocks"

BlockMeasurement = namedtuple("BlockMeasurement",
                              "source_key profiles seconds written_bytes workers")

# What the recorded runs say each block costs: the profiles' pooled seconds, and the bytes written.
BlockCosts = namedtuple("BlockCosts", "seconds written_bytes")

NO_BLOCK_COSTS = BlockCosts(seconds={}, written_bytes={})


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


def _summed(block_rows, field):
    """Sum one per-block field across every worker that reported it.

    A block can be split across workers -- one block bigger than a worker's
    share, or a run of one blob cut by a pool chunk -- so the run's figure for
    it is the sum of what each worker spent there.

    Args:
        block_rows: The run's `scope=blocks` rows.
        field: The field to sum.

    Returns:
        A mapping of block key to the summed figure, or None where any row
        lacks the field: a partial set is a biased sample.
    """
    if any(field not in row for row in block_rows):
        return None
    totals = {}
    for row in block_rows:
        for block, value in parse_block_values(row[field]).items():
            totals[block] = totals.get(block, 0.0) + value
    return totals


def measure_blocks(rows):
    """Sum every worker's per-block seconds and bytes into the run's.

    Args:
        rows: Parsed usage rows for the whole run.

    Returns:
        The run's BlockMeasurement, or None where no worker reported blocks.
        Its `written_bytes` is None where a worker reported seconds alone.

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
    return BlockMeasurement(source_key=source_key, profiles=profiles,
                            seconds=_summed(block_rows, "seconds") or {},
                            written_bytes=_summed(block_rows, "written_bytes"),
                            workers=len(block_rows))


def empty_document(source_key, profiles):
    """A block document holding no measurement at all.

    Args:
        source_key: The archive the document is for.
        profiles: The profile set it is for.

    Returns:
        The document.
    """
    return {"version": VERSION, "source_key": source_key, "profiles": profiles,
            "seconds": {}, "written_bytes": {}}


def _append(histories, measured, digits):
    """Append one run's figure to each block's history.

    Args:
        histories: The block-keyed histories, mutated in place.
        measured: The run's figure per block.
        digits: How many decimals to keep, None for a whole number.
    """
    for block, value in measured.items():
        history = histories.get(str(block), [])
        histories[str(block)] = (history + [round(value, digits)])[-HISTORY_LENGTH:]


def record_blocks(document, measurement, build=None):
    """Append one run's per-block seconds and bytes to the document's history.

    Every block keeps its own last `HISTORY_LENGTH` observations, the same
    depth the axis coefficients keep. A block this run did not cover keeps the
    history it had: a z0..z11 run says nothing about z14. A document of an
    older version is started afresh, its blocks having been keyed by
    something else.

    Args:
        document: The document to extend, mutated in place.
        measurement: The run's BlockMeasurement.
        build: The archive build the run read, kept for a reader.

    Returns:
        A line saying what was recorded, for the job log.
    """
    if document.get("version") != VERSION:
        document.clear()
        document.update(empty_document(measurement.source_key, measurement.profiles))
    seconds = document.setdefault("seconds", {})
    # Milliseconds: what a block costs is never balanced finer than that.
    _append(seconds, measurement.seconds, 3)
    if measurement.written_bytes is not None:
        _append(document.setdefault("written_bytes", {}), measurement.written_bytes, None)
    if build:
        document["build"] = build
    recorded_bytes = ("and their written bytes" if measurement.written_bytes is not None
                      else "but no written bytes, which not every worker reported")
    return (f"blocks {measurement.source_key}/{measurement.profiles}: recorded "
            f"{len(measurement.seconds)} blocks' seconds {recorded_bytes}, from "
            f"{measurement.workers} workers, {len(seconds)} on file")


def _medians(histories):
    """The median of each block's history.

    Args:
        histories: The block-keyed histories as read.

    Returns:
        A mapping of block key to its median, skipping anything in the file
        that is not a list of non-negative numbers.
    """
    medians = {}
    for block, history in histories.items():
        values = [float(value) for value in history if isinstance(value, (int, float))
                  and value >= 0] if isinstance(history, list) else []
        if values:
            medians[int(block)] = statistics.median(values)
    return medians


def block_costs(document):
    """What the recorded runs say each block costs.

    Args:
        document: The block document.

    Returns:
        The BlockCosts, empty for a document of an older version, whose
        blocks were keyed by something else.
    """
    if document.get("version") != VERSION:
        return NO_BLOCK_COSTS
    return BlockCosts(seconds=_medians(document.get("seconds", {})),
                      written_bytes=_medians(document.get("written_bytes", {})))


def read_block_costs(root, source_key, profiles):
    """Read the medians for one archive and profile set out of a checkout.

    Args:
        root: The checked-out block state directory, or None.
        source_key: The archive the run reads.
        profiles: The profile set the run builds.

    Returns:
        The BlockCosts, empty where there is no directory or no file for this
        pair -- the first run of anything has nothing measured yet.

    Raises:
        ValueError: If the file exists but is not a JSON object.
    """
    if not root:
        return NO_BLOCK_COSTS
    path = block_state_path(root, source_key, profiles)
    if not os.path.exists(path):
        return NO_BLOCK_COSTS
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{path} holds {type(document).__name__}, not a JSON object")
    return block_costs(document)
