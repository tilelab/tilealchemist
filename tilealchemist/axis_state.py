"""The measured axes a run keeps for the next one; see docs/ARCHITECTURE.md "Measuring a run"."""
import statistics

from tilealchemist.calibration import (
    REVIEWED_SHARED,
    REVIEWED_SOURCE,
    SharedAxes,
    SourceAxes,
    adopt_group,
    axis_seconds_of,
)

# How many runs one coefficient keeps. Five is enough for a median to ignore one bad runner.
HISTORY_LENGTH = 5

VERSION = 1


def empty_document():
    """A state document holding no measurement at all.

    Returns:
        The document, which every reader below treats as "nothing measured
        yet" and therefore falls back to the reviewed coefficients on.
    """
    return {"version": VERSION, "sources": {}, "shared": {}}


def _observations(entry, name):
    """One coefficient's recorded runs.

    Args:
        entry: The document section holding that coefficient.
        name: The coefficient's name.

    Returns:
        The finite, non-negative observations, oldest first. Anything else in
        the file is dropped rather than trusted: a null or a negative cost
        would drag a median to nonsense. Zero is kept, because it is a
        measurement -- a decode fit that finds no per-call cost says so with
        a zero, and dropping it left `decode_call` forever unmeasured.
    """
    raw = entry.get(name) if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return []
    return [float(value) for value in raw
            if isinstance(value, (int, float)) and value >= 0 and value == value]


def _summarize(entry, name):
    """What the recorded runs say this coefficient is.

    The median, not the mean: one runner with a slow disk, or one archive
    having a bad afternoon, should not move the figure the next run is
    partitioned by, and a mean of five lets a single spike through at a fifth
    of its full weight.

    Args:
        entry: The document section holding that coefficient.
        name: The coefficient's name.

    Returns:
        The median observation, or None where nothing was recorded.
    """
    values = _observations(entry, name)
    return statistics.median(values) if values else None


def _record_group(entry, measured):
    """Append one run's measurements to a document section.

    Args:
        entry: The section to extend, mutated in place.
        measured: The measured group, whose None fields are skipped: a
            coefficient this run could not measure keeps the history it had
            rather than gaining a hole.

    Returns:
        The names actually recorded.
    """
    recorded = []
    for name in measured._fields:
        value = getattr(measured, name)
        if value is None or value < 0 or value != value:
            continue
        entry[name] = (_observations(entry, name) + [float(value)])[-HISTORY_LENGTH:]
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
    document.setdefault("version", VERSION)
    # A profile's cost is the block state's to keep, per archive and profile set; see block_state.py.
    document.pop("profiles", None)
    lines = []
    if measurement.source_key:
        entry = document.setdefault("sources", {}).setdefault(measurement.source_key, {})
        if build:
            entry["build"] = build
        recorded = _record_group(entry, measurement.source)
        lines.append(f"source {measurement.source_key}: recorded {', '.join(recorded) or 'nothing'}")
    recorded = _record_group(document.setdefault("shared", {}), measurement.shared)
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
    entry = document.get("sources", {}).get(source_key, {})
    measured = SourceAxes(**{name: _summarize(entry, name) for name in SourceAxes._fields})
    return adopt_group(measured, REVIEWED_SOURCE, notes, prefix=f"{source_key}.")


def shared_axes(document, notes):
    """What the recorded runs say tilealchemist's own work and the runner cost.

    Args:
        document: The state document.
        notes: The list any fallback explanation is appended to.

    Returns:
        The SharedAxes to charge, every field usable.
    """
    entry = document.get("shared", {})
    measured = SharedAxes(**{name: _summarize(entry, name) for name in SharedAxes._fields})
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
    return axis_seconds_of(source_axes(document, source_key, notes), shared), shared


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
    depths = []
    for entry in document.get("sources", {}).values():
        depths.extend(len(_observations(entry, name)) for name in SourceAxes._fields
                      if name in entry)
    shared = document.get("shared", {})
    depths.extend(len(_observations(shared, name)) for name in SharedAxes._fields
                  if name in shared)
    return min(depths) if depths else 0
