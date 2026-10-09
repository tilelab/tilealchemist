"""Fitting the next run's cost coefficients from the last run's usage lines."""
import json
import math
from collections import namedtuple

from tilealchemist.cost import (AXIS_SECONDS, DEFAULT_TRANSFORM_PARALLELISM,
                                WORKER_SETUP_SECONDS, AxisSeconds)

# What the archive costs: its fetch rate, and what its tiles cost to decode.
SourceAxes = namedtuple("SourceAxes", "fetched_byte decode_call decoded_byte")

# What belongs to neither: tilealchemist's own bookkeeping, the runner's disk,
# and its cores.
SharedAxes = namedtuple(
    "SharedAxes",
    "manifest_entry written_byte worker_setup_seconds transform_parallelism")

REVIEWED_SOURCE = SourceAxes(fetched_byte=AXIS_SECONDS.fetched_byte,
                             decode_call=AXIS_SECONDS.decode_call,
                             decoded_byte=AXIS_SECONDS.decoded_byte)

REVIEWED_SHARED = SharedAxes(
    manifest_entry=AXIS_SECONDS.manifest_entry,
    written_byte=AXIS_SECONDS.written_byte,
    worker_setup_seconds=WORKER_SETUP_SECONDS,
    transform_parallelism=DEFAULT_TRANSFORM_PARALLELISM)

RunMeasurement = namedtuple(
    "RunMeasurement", "source_key source shared diagnostics notes")


def parse_usage_lines(lines):
    """Pull the `usage:` lines out of a run's logs.

    Args:
        lines: The log lines to read; everything else is ignored.

    Returns:
        One mapping of field name to raw string value per usage line, in the
        order they appeared.
    """
    rows = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("usage: "):
            continue
        fields = {}
        for token in stripped[len("usage: "):].split(" "):
            name, separator, value = token.partition("=")
            if separator:
                fields[name] = value
        rows.append(fields)
    return rows


def _scoped(rows, scope):
    """The rows belonging to one scope.

    Args:
        rows: Parsed usage rows.
        scope: The scope to keep, such as "worker" or "profile".

    Returns:
        The matching rows, in order.
    """
    return [row for row in rows if row.get("scope") == scope]


def _total(rows, field):
    """Sum one field across the rows that carry it.

    Args:
        rows: Parsed usage rows.
        field: The field to sum.

    Returns:
        The total, 0.0 where no row carries that field.
    """
    return sum(float(row[field]) for row in rows if field in row)


def _ratio(seconds, units):
    """Seconds per unit, aggregated.

    An aggregate ratio, never the mean of per-unit rates: a small unit
    carries the same fixed overhead as a large one, so averaging the rates
    would let the smallest units set the figure.

    Args:
        seconds: The total seconds spent.
        units: The total units they went on.

    Returns:
        The seconds per unit, or None where there were no units.
    """
    return seconds / units if units else None


def length_buckets(worker_rows):
    """Total the per-length histograms across every worker.

    Args:
        worker_rows: Parsed usage rows for scope "worker".

    Returns:
        A mapping of bit length to `[calls, bytes, decode]`.
    """
    totals = {}
    for row in worker_rows:
        histogram = row.get("length_hist", "-")
        if histogram == "-":
            continue
        for item in histogram.split("|"):
            bits, count, byte_count, decode = item.split(":")
            bucket = totals.setdefault(int(bits), [0, 0, 0.0])
            bucket[0] += int(count)
            bucket[1] += int(byte_count)
            bucket[2] += float(decode)
    return totals


def _fit_two(samples):
    """Least-squares fit of two terms, no intercept, neither term negative.

    Both terms are costs, so a negative one is not a finding but the fit
    buying a better line with a price nobody pays. Decode is the case that
    forces it: GEOS work grows faster than the byte length, and an
    unconstrained fit answers that curve with a negative per-call cost --
    -0.00127s on worker 5 of standardprofiles run 36608758128 -- which the
    state then discarded, so `decode_call` was never measured at all. Where
    the unconstrained optimum leaves the positive quadrant, the best fit with
    one term at zero is taken instead, and zero is a measurement like any
    other.

    Args:
        samples: The `(x1, x2, target)` triples to fit.

    Returns:
        The two coefficients, or None where the samples do not determine
        them.
    """
    left = right = cross = first = second = 0.0
    for x_one, x_two, target in samples:
        left += x_one * x_one
        cross += x_one * x_two
        right += x_two * x_two
        first += x_one * target
        second += x_two * target
    determinant = left * right - cross * cross
    if determinant <= 0:
        return None
    one = (first * right - second * cross) / determinant
    two = (second * left - first * cross) / determinant
    if one >= 0 and two >= 0:
        return one, two

    def excess(one, two):
        """The squared error of a candidate, less the shared constant."""
        return (one * one * left + 2 * one * two * cross + two * two * right
                - 2 * (one * first + two * second))

    return min(((max(first / left, 0.0), 0.0), (0.0, max(second / right, 0.0))),
               key=lambda pair: excess(*pair))


def _entry_samples(buckets):
    """Turn the length buckets into samples for the per-entry decode fit.

    The byte term is the bucket's byte total outright: decode is charged on
    length itself, so `calls * mean length` is just that total again. The
    target is the decode alone -- the profiles' own seconds are attributed to
    the profiles that spent them, not to the archive.

    Args:
        buckets: The totalled histograms, by bit length.

    Returns:
        `(calls, bytes, decode_seconds)` per non-empty bucket.
    """
    return [(count, byte_count, decode)
            for count, byte_count, decode in buckets.values() if count]


def _adopted(name, measured, reviewed, notes):
    """Take the measured coefficient, or the reviewed one where there is none.

    A measurement is trusted however far it lands from the reviewed value:
    the recorded runs are already a median over several of them, and pulling
    an honest figure back towards a guess only hides that the guess was wrong.

    Args:
        name: The coefficient's name, for the note.
        measured: What the recorded runs say, or None.
        reviewed: The value the reviewed cost model carries.
        notes: The list any explanation is appended to.

    Returns:
        The value to use.
    """
    if measured is None or not math.isfinite(measured) or measured < 0:
        notes.append(f"{name}: nothing usable measured, keeping the reviewed "
                     f"{reviewed:g}")
        return reviewed
    return measured


def source_key_of(rows):
    """Which archive this run's workers read.

    Args:
        rows: Parsed usage rows for the whole run.

    Returns:
        The one key every worker reported, or None where they disagree or none
        said. Disagreement means two runs' logs were concatenated, and no fit
        should treat that as one archive.
    """
    keys = {row["source_key"] for row in _scoped(rows, "worker")
            if "source_key" in row}
    return keys.pop() if len(keys) == 1 else None


def measure_source(rows):
    """Fit what the archive costs, from the workers that read it.

    Args:
        rows: Parsed usage rows for the whole run.

    Returns:
        The measured SourceAxes, any field None where nothing usable was
        measured.
    """
    workers = _scoped(rows, "worker")
    fetched = _ratio(_total(workers, "fetch_seconds"),
                     _total(workers, "fetched_bytes"))
    decode_samples = _entry_samples(length_buckets(workers))
    decode_call, decoded_byte = _fit_two(decode_samples) or (None, None)
    return SourceAxes(fetched_byte=fetched, decode_call=decode_call,
                      decoded_byte=decoded_byte)


def measure_transform_parallelism(rows):
    """Fit how many seconds of in-pool work one second of wall clock buys.

    `run_transform()` fans a batch out across `--transform-processes`
    processes, and every measurement taken inside one of them --
    `length_hist`'s decode seconds, and the profiles' `profile_seconds` --
    comes back summed across the pool. The worker's own `transform` phase is
    the wall clock those seconds were spent in, so their ratio is what the
    pool actually bought, pool overhead and stragglers already deducted.
    Charging the summed figure to a worker's wall-clock budget without it
    over-predicts every run by about that factor.

    Args:
        rows: Parsed usage rows for the whole run.

    Returns:
        The ratio, or None where nothing usable was measured. It is deliberately
        not clamped to the process count: a pool that never pays off is a
        measurement, not an error.
    """
    workers = _scoped(rows, "worker")
    pooled = (sum(bucket[2] for bucket in length_buckets(workers).values())
              + _total(workers, "profile_seconds"))
    # It divides every pooled second, so a run that pooled none has measured
    # nothing.
    if not pooled:
        return None
    return _ratio(pooled, _total(workers, "transform_seconds"))


def measure_shared(rows, runner_overhead_seconds=None):
    """Fit what belongs to neither the archive nor a profile.

    Args:
        rows: Parsed usage rows for the whole run.
        runner_overhead_seconds: What a runner costs before the worker's own
            code starts, which no log of that worker can see. None leaves the
            setup figure unmeasured.

    Returns:
        The measured SharedAxes, any field None where nothing usable was
        measured.
    """
    workers, profiles = _scoped(rows, "worker"), _scoped(rows, "profile")
    setup_samples = [
        (1.0,
         float(row.get("real_entries", 0)) + float(row.get("gap_entries", 0)),
         float(row["setup_seconds"]))
        for row in workers if "setup_seconds" in row]
    # The in-process residual only: a log cannot see runner boot, artifact
    # download or pip.
    in_process_setup, manifest_entry = _fit_two(setup_samples) or (None, None)
    # Payload bytes, real and gap alike: both were written, and both took time.
    written_bytes = (_total(workers, "output_bytes")
                     + _total(profiles, "gap_bytes"))
    return SharedAxes(
        manifest_entry=manifest_entry,
        written_byte=_ratio(_total(workers, "write_seconds"), written_bytes),
        worker_setup_seconds=(None if in_process_setup is None
                              or runner_overhead_seconds is None
                              else runner_overhead_seconds + in_process_setup),
        transform_parallelism=measure_transform_parallelism(rows))


def measure_run(rows, runner_overhead_seconds=None):
    """Measure one run, split by what each coefficient belongs to.

    Nothing falls back here: this is what the run says, and the reviewed
    model only stands in where a value is adopted.

    Args:
        rows: Parsed usage rows for the whole run.
        runner_overhead_seconds: What a runner costs before the worker's own
            code starts. None leaves the setup figure unmeasured.

    Returns:
        The RunMeasurement, whose notes say what could not be measured.
    """
    workers, profiles = _scoped(rows, "worker"), _scoped(rows, "profile")
    buckets = length_buckets(workers)
    decode_total = sum(bucket[2] for bucket in buckets.values())
    transform_total = _total(workers, "profile_seconds")
    notes = []
    key = source_key_of(rows)
    if key is None:
        notes.append("source_key: the workers do not agree on one archive, so "
                     "the archive's own coefficients cannot be filed")
    if runner_overhead_seconds is None:
        notes.append("worker_setup_seconds: a worker's log cannot see "
                     "runner boot, artifact download or pip install, so this "
                     "needs the runner overhead, which merge-axes reads off "
                     "the run's job timings given `actions: read`")
    diagnostics = {
        "workers": len(workers),
        "profile_rows": len(profiles),
        "decode_share": _ratio(decode_total, decode_total + transform_total),
        "decode_seconds": decode_total,
        "transform_seconds": transform_total,
    }
    return RunMeasurement(source_key=key, source=measure_source(rows),
                          shared=measure_shared(rows, runner_overhead_seconds),
                          diagnostics=diagnostics, notes=notes)


def adopt_group(measured, reviewed, notes, prefix=""):
    """Settle every field of one measured group against the reviewed one.

    Args:
        measured: The measured group, whose fields may be None.
        reviewed: The reviewed group of the same type.
        notes: The list any explanation is appended to.
        prefix: Prepended to each coefficient's name in a note, so that a note
            says which archive it is about.

    Returns:
        A group of the same type, every field usable.
    """
    return type(reviewed)(**{
        name: _adopted(prefix + name, getattr(measured, name),
                       getattr(reviewed, name), notes)
        for name in reviewed._fields})


def axis_seconds_of(source, shared):
    """Assemble the five cost-model coefficients from two of the groups.

    Args:
        source: The archive's coefficients.
        shared: The coefficients belonging to neither archive nor profile.

    Returns:
        The AxisSeconds the cost model charges. A profile's own cost is not
        here: the block state measures it per tile block.
    """
    return AxisSeconds(manifest_entry=shared.manifest_entry,
                       decode_call=source.decode_call,
                       fetched_byte=source.fetched_byte,
                       decoded_byte=source.decoded_byte,
                       written_byte=shared.written_byte)


def axis_seconds_from_json(data):
    """Read the per-axis seconds out of a calibration document.

    Args:
        data: The parsed calibration JSON, either whole or just its
            `axis_seconds`.

    Returns:
        The coefficients as an AxisSeconds.

    Raises:
        ValueError: If the document is missing any of them.
    """
    axis = data["axis_seconds"] if "axis_seconds" in data else data
    missing = [name for name in AxisSeconds._fields if name not in axis]
    if missing:
        raise ValueError(f"no {', '.join(missing)} in the calibration; it "
                         f"must carry all {len(AxisSeconds._fields)} "
                         f"coefficients")
    return AxisSeconds(**{name: float(axis[name])
                          for name in AxisSeconds._fields})


def load_calibration_file(path):
    """Read a flat calibration file, as `tilealchemist-calibrate` writes it.

    Args:
        path: The file to read.

    Returns:
        Its per-axis seconds.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not valid JSON, or is missing a coefficient.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return axis_seconds_from_json(data)


def _group_lines(title, measured, reviewed):
    """Render one group's measurements against the reviewed values.

    Args:
        title: What the group is, for the heading.
        measured: The measured group, whose fields may be None.
        reviewed: The reviewed group of the same type.

    Returns:
        The heading and one line per coefficient.
    """
    lines = [f"{title}:"]
    for name in reviewed._fields:
        value = getattr(measured, name)
        shown = "n/a" if value is None else f"{value:.4g}"
        lines.append(f"  {name:<24}{getattr(reviewed, name):>14.4g}{shown:>14}")
    return lines


def proposal_lines(measurement):
    """Render a whole run's measurements, grouped by what each belongs to.

    Args:
        measurement: The run's RunMeasurement.

    Returns:
        The lines to print, header first.
    """
    lines = [f"{'coefficient':<26}{'reviewed':>14}{'measured':>14}"]
    lines.extend(_group_lines(f"source {measurement.source_key}",
                              measurement.source, REVIEWED_SOURCE))
    lines.extend(_group_lines("shared", measurement.shared, REVIEWED_SHARED))
    share = measurement.diagnostics.get("decode_share")
    if share is not None:
        lines.append(f"decode is {share:.1%} of per-entry CPU, the profiles "
                     f"{1 - share:.1%}")
    return lines
