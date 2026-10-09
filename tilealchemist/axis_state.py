"""The measured axes a run keeps for the next one.

See docs/ARCHITECTURE.md "Measuring a run".
"""
import json
import os
import statistics

from tilealchemist.calibration import (
    REVIEWED_SHARED,
    REVIEWED_SOURCE,
    SharedAxes,
    SourceAxes,
    adopt_group,
    axis_seconds_of,
)

# How many runs one coefficient keeps. Five is enough for a median to ignore one
# bad runner.
HISTORY_LENGTH = 5

# 2 renamed `manifest_record` to `manifest_entry`.
VERSION = 2


def empty_document():
    """A state document holding no measurement at all.

    Returns:
        The document, which every reader below treats as "nothing measured
        yet" and therefore falls back to the reviewed coefficients on.
    """
    return {"version": VERSION, "sources": {}, "shared": {}}


def read_state_file(path):
    """Read a state document out of a checkout of the state branch.

    Args:
        path: The file to read, or None where the run was given none.

    Returns:
        The parsed document, or None where there is no path or no file. A
        missing file is not a mistake: the first run of a new repository has
        no state branch yet, and the first run of anything has nothing
        measured.

    Raises:
        ValueError: If the file exists but does not hold a JSON object, which
            would otherwise silently cost the run its calibration.
    """
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        kind = type(document).__name__
        raise ValueError(f"{path} holds {kind}, not a JSON object")
    return document


def upgrade_document(document):
    """Bring a document written by an older version up to this one, in place.

    Args:
        document: The state document as read, mutated in place.

    Returns:
        The same document, for a caller to chain.
    """
    if document.get("version", 1) < 2:
        shared = document.get("shared")
        if isinstance(shared, dict) and "manifest_record" in shared:
            shared.setdefault("manifest_entry", shared.pop("manifest_record"))
        document["version"] = VERSION
    return document


def _is_observation(value):
    """Whether a value can join a coefficient's history.

    Args:
        value: The candidate, as measured or as read from the file.

    Returns:
        True for a non-negative number that is not NaN, zero included.
    """
    return isinstance(value, (int, float)) and value >= 0 and value == value


def _observations(section, name):
    """One coefficient's recorded runs.

    Args:
        section: The document section holding that coefficient.
        name: The coefficient's name.

    Returns:
        The finite, non-negative observations, oldest first. Anything else in
        the file is dropped rather than trusted: a null or a negative cost
        would drag a median to nonsense. Zero is kept, because it is a
        measurement -- a decode fit that finds no per-call cost says so with
        a zero, and dropping it left `decode_call` forever unmeasured.
    """
    raw = section.get(name) if isinstance(section, dict) else None
    if not isinstance(raw, list):
        return []
    return [float(value) for value in raw if _is_observation(value)]


def _summarize(section, name):
    """What the recorded runs say this coefficient is.

    The median, not the mean: one runner with a slow disk, or one archive
    having a bad afternoon, should not move the figure the next run is
    partitioned by, and a mean of five lets a single spike through at a fifth
    of its full weight.

    Args:
        section: The document section holding that coefficient.
        name: The coefficient's name.

    Returns:
        The median observation, or None where nothing was recorded.
    """
    values = _observations(section, name)
    return statistics.median(values) if values else None


def _summarize_group(group_type, section):
    """What the recorded runs say every coefficient of one group is.

    Args:
        group_type: The group's namedtuple type, SourceAxes or SharedAxes.
        section: The document section holding the group's coefficients.

    Returns:
        A group of that type, any field None where nothing was recorded.
    """
    return group_type(**{name: _summarize(section, name)
                         for name in group_type._fields})


def _record_group(section, measured):
    """Append one run's measurements to a document section.

    Args:
        section: The section to extend, mutated in place.
        measured: The measured group, whose None fields are skipped: a
            coefficient this run could not measure keeps the history it had
            rather than gaining a hole.

    Returns:
        The names actually recorded.
    """
    recorded = []
    for name in measured._fields:
        value = getattr(measured, name)
        if not _is_observation(value):
            continue
        history = _observations(section, name) + [float(value)]
        section[name] = history[-HISTORY_LENGTH:]
        recorded.append(name)
    return recorded


def record_run(document, measurement, build=None):
    """Fold one run's measurements into the state document.

    Args:
        document: The document to extend, mutated in place.
        measurement: The run's RunMeasurement.
        build: The archive build this run read, kept alongside the archive's
            coefficients for a reader to see which extract last moved them.
            It is deliberately not part of the key; `SourceMetadata.axis_key`
            says why.

    Returns:
        A line per section saying what was recorded, for the job log.
    """
    upgrade_document(document)
    # A profile's cost is the block state's to keep, per archive and profile
    # set; see block_state.py.
    document.pop("profiles", None)
    lines = []
    if measurement.source_key:
        section = document.setdefault("sources", {}).setdefault(
            measurement.source_key, {})
        if build:
            section["build"] = build
        recorded = _record_group(section, measurement.source)
        lines.append(f"source {measurement.source_key}: recorded "
                     f"{', '.join(recorded) or 'nothing'}")
    recorded = _record_group(document.setdefault("shared", {}),
                             measurement.shared)
    lines.append(f"shared: recorded {', '.join(recorded) or 'nothing'}")
    return lines


def source_axes(document, source_key, notes):
    """What the archive's recorded runs say it costs.

    Args:
        document: The state document.
        source_key: The archive's key, as `SourceMetadata.axis_key` gives it.
        notes: The list any fallback explanation is appended to.

    Returns:
        The SourceAxes to charge, every field usable.
    """
    section = document.get("sources", {}).get(source_key, {})
    measured = _summarize_group(SourceAxes, section)
    return adopt_group(measured, REVIEWED_SOURCE, notes,
                       prefix=f"{source_key}.")


def shared_axes(document, notes):
    """What the recorded runs say tilealchemist's own work and the runner cost.

    Args:
        document: The state document.
        notes: The list any fallback explanation is appended to.

    Returns:
        The SharedAxes to charge, every field usable.
    """
    upgrade_document(document)
    measured = _summarize_group(SharedAxes, document.get("shared", {}))
    return adopt_group(measured, REVIEWED_SHARED, notes)


def axis_seconds(document, source_key, notes):
    """The five cost-model coefficients this document implies.

    Args:
        document: The state document.
        source_key: The archive the next run will read.
        notes: The list any fallback explanation is appended to.

    Returns:
        The AxisSeconds to charge, and the shared group it was built from, so
        that a caller also has the setup seconds.
    """
    shared = shared_axes(document, notes)
    source = source_axes(document, source_key, notes)
    return axis_seconds_of(source, shared), shared


def history_depth(document):
    """How many runs the shallowest recorded coefficient rests on.

    A caller adopting these values wants to know whether it is trusting one
    run or five, so this reports the weakest link rather than the best.

    Args:
        document: The state document.

    Returns:
        The smallest number of observations behind any recorded coefficient,
        and zero where nothing is recorded at all.
    """
    upgrade_document(document)
    depths = []
    for section in document.get("sources", {}).values():
        depths.extend(len(_observations(section, name))
                      for name in SourceAxes._fields if name in section)
    shared = document.get("shared", {})
    depths.extend(len(_observations(shared, name))
                  for name in SharedAxes._fields if name in shared)
    return min(depths) if depths else 0
