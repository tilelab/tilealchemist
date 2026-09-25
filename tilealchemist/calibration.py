"""Fitting the next run's cost coefficients from the last run's `usage:` lines."""
import json
import math
from collections import namedtuple

from tilealchemist.cost import (
    AXIS_SECONDS,
    DENSITY_EXPONENT,
    WORKER_SETUP_SECONDS,
    AxisSeconds,
)

# A measured coefficient may differ from the reviewed one by this factor, and no further.
CLAMP_FACTOR = 4.0

MIN_SCORED_WORKERS = 8

DENSITY_CANDIDATES = tuple(round(1.0 + step / 10, 1) for step in range(11))

Calibration = namedtuple(
    "Calibration",
    "axis_seconds worker_setup_seconds density_exponent runner diagnostics notes")

RunnerProfile = namedtuple("RunnerProfile", "rss_base rss_per_batch_byte bytes_per_output_tile")

# What a worker's peak RSS and shard size come to, before any measurement replaces them.
DEFAULT_RUNNER = RunnerProfile(rss_base=400 * 1024 * 1024, rss_per_batch_byte=4.6,
                               bytes_per_output_tile=250.0)


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
        scope: The scope to keep, such as "worker" or "chunk".

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


def length_buckets(chunk_rows):
    """Total the per-length histograms across every chunk.

    Args:
        chunk_rows: Parsed usage rows for scope "chunk".

    Returns:
        A mapping of bit length to `[calls, bytes, decode, transform]`.
    """
    totals = {}
    for row in chunk_rows:
        histogram = row.get("length_hist", "-")
        if histogram == "-":
            continue
        for item in histogram.split("|"):
            bits, count, byte_count, decode, transform = item.split(":")
            bucket = totals.setdefault(int(bits), [0, 0, 0.0, 0.0])
            bucket[0] += int(count)
            bucket[1] += int(byte_count)
            bucket[2] += float(decode)
            bucket[3] += float(transform)
    return totals


def _fit_two(samples):
    """Least-squares fit of a two-term model with no intercept.

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
    return ((first * right - second * cross) / determinant,
            (second * left - first * cross) / determinant)


def _entry_samples(buckets, density_exponent):
    """Turn the length buckets into samples for the per-entry fit.

    Args:
        buckets: The totalled histograms, by bit length.
        density_exponent: The power the decode cost is charged on length at.

    Returns:
        `(calls, weighted bytes, seconds)` per non-empty bucket.
    """
    samples = []
    for count, byte_count, decode, transform in buckets.values():
        if not count:
            continue
        mean_length = byte_count / count
        samples.append((count, count * mean_length ** density_exponent, decode + transform))
    return samples


def _residual(samples, fit):
    """The squared error a fit leaves on its samples.

    Args:
        samples: The `(x1, x2, target)` triples that were fitted.
        fit: The two coefficients, or None where there was no fit.

    Returns:
        The sum of squared residuals, or infinity where there was no fit.
    """
    if fit is None:
        return math.inf
    call_cost, byte_cost = fit
    return sum((target - call_cost * x_one - byte_cost * x_two) ** 2
               for x_one, x_two, target in samples)


def best_density_exponent(buckets, candidates=DENSITY_CANDIDATES):
    """The exponent whose fit leaves the least error.

    Args:
        buckets: The totalled histograms, by bit length.
        candidates: The exponents to try.

    Returns:
        The best candidate, or None where there were none to try.
    """
    scored = []
    for candidate in candidates:
        samples = _entry_samples(buckets, candidate)
        scored.append((_residual(samples, _fit_two(samples)), candidate))
    return min(scored)[1] if scored else None


def correlation(left, right):
    """Pearson correlation between two equal-length series.

    Args:
        left: One series.
        right: The other, in the same order.

    Returns:
        The coefficient, or None where there are too few workers to score it
        or either series does not vary at all.
    """
    count = len(left)
    if count < MIN_SCORED_WORKERS:
        return None
    mean_left, mean_right = sum(left) / count, sum(right) / count
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    spread = math.sqrt(sum((a - mean_left) ** 2 for a in left)
                       * sum((b - mean_right) ** 2 for b in right))
    return covariance / spread if spread else None


def _clamped(name, measured, reviewed, notes):
    """Keep a measured coefficient within reach of the reviewed one.

    One odd run should move a coefficient, not replace it, so a measurement
    further than CLAMP_FACTOR from the reviewed value is pulled back to that
    bound and the reason recorded.

    Args:
        name: The coefficient's name, for the note.
        measured: What this run measured, or None.
        reviewed: The value the reviewed cost model carries.
        notes: The list any explanation is appended to.

    Returns:
        The value to use.
    """
    if measured is None or not math.isfinite(measured) or measured <= 0:
        notes.append(f"{name}: nothing usable measured, keeping the reviewed {reviewed:g}")
        return reviewed
    low, high = reviewed / CLAMP_FACTOR, reviewed * CLAMP_FACTOR
    if not low <= measured <= high:
        clamped = min(max(measured, low), high)
        notes.append(f"{name}: measured {measured:.4g} is outside {CLAMP_FACTOR:g}x of the "
                     f"reviewed {reviewed:g}, clamped to {clamped:.4g}")
        return clamped
    return measured


def fit_runner_profile(rows, fallback=DEFAULT_RUNNER):
    """Fit a runner's memory and disk rates from a run's logs.

    Args:
        rows: Parsed usage rows for the whole run.
        fallback: The rates to keep where nothing usable was measured.

    Returns:
        The fitted RunnerProfile.
    """
    workers, chunks, profiles = _scoped(rows, "worker"), _scoped(rows, "chunk"), _scoped(
        rows, "profile")
    rss_fit = _fit_two([(1.0, float(row["peak_batch_bytes"]), float(row["peak_rss"]))
                        for row in workers
                        if float(row.get("peak_batch_bytes", 0)) > 0 and "peak_rss" in row])
    per_tile = _ratio(_total(profiles, "shard_bytes"), _total(chunks, "output_tiles"))
    return RunnerProfile(
        rss_base=rss_fit[0] if rss_fit and rss_fit[0] > 0 else fallback.rss_base,
        rss_per_batch_byte=(rss_fit[1] if rss_fit and rss_fit[1] > 0
                            else fallback.rss_per_batch_byte),
        bytes_per_output_tile=per_tile if per_tile else fallback.bytes_per_output_tile)


def runner_profile_from_json(data):
    """Read a runner profile out of a calibration document.

    Args:
        data: The parsed calibration JSON.

    Returns:
        The RunnerProfile it carries, each field defaulted where absent.
    """
    runner = data.get("runner", {}) if isinstance(data, dict) else {}
    return RunnerProfile(**{name: float(runner[name]) if name in runner
                            else getattr(DEFAULT_RUNNER, name)
                            for name in RunnerProfile._fields})


def calibrate(rows, reviewed=AXIS_SECONDS, reviewed_setup=WORKER_SETUP_SECONDS,
              density_exponent=DENSITY_EXPONENT, runner_overhead_seconds=None):
    """Fit the next run's cost coefficients from the last run's logs.

    Every coefficient is measured and then clamped towards the reviewed one,
    so that a single unusual run moves the model without taking it over.
    What was clamped, and why, comes back in the notes.

    Args:
        rows: Parsed usage rows for the whole run.
        reviewed: The reviewed per-axis seconds to measure against.
        reviewed_setup: The reviewed per-worker setup seconds.
        density_exponent: The power the decode cost is charged on length at.
        runner_overhead_seconds: What a runner costs before the worker's own
            code starts, which no log of that worker can see. None leaves the
            reviewed setup figure alone, and says so in the notes.

    Returns:
        The fitted Calibration, with its diagnostics and its notes.
    """
    workers, chunks = _scoped(rows, "worker"), _scoped(rows, "chunk")
    notes, diagnostics = [], {}

    fetched = _ratio(_total(workers, "fetch_seconds"), _total(workers, "fetched_bytes"))
    output = _ratio(_total(workers, "write_seconds"), _total(chunks, "output_tiles"))

    buckets = length_buckets(chunks)
    entry_fit = _fit_two(_entry_samples(buckets, density_exponent))
    setup_fit = _fit_two([(1.0, float(row.get("real_entries", 0)) + float(
        row.get("gap_entries", 0)), float(row["setup_seconds"]))
        for row in workers if "setup_seconds" in row])

    decode_total = sum(bucket[2] for bucket in buckets.values())
    transform_total = sum(bucket[3] for bucket in buckets.values())
    diagnostics["workers"] = len(workers)
    diagnostics["chunks"] = len(chunks)
    diagnostics["decode_share"] = _ratio(decode_total, decode_total + transform_total)
    diagnostics["best_density_exponent"] = best_density_exponent(buckets)
    diagnostics["measured"] = {
        "manifest_record": setup_fit[1] if setup_fit else None,
        "decode_call": entry_fit[0] if entry_fit else None,
        "fetched_byte": fetched,
        "decoded_byte": entry_fit[1] if entry_fit else None,
        "output_tile": output,
    }
    # The in-process residual only: a log cannot see runner boot, artifact download or pip.
    in_process_setup = setup_fit[0] if setup_fit else None
    diagnostics["in_process_setup_seconds"] = in_process_setup
    diagnostics["measured"]["worker_setup_seconds"] = (
        None if in_process_setup is None or runner_overhead_seconds is None
        else runner_overhead_seconds + in_process_setup)

    axis = AxisSeconds(**{name: _clamped(name, diagnostics["measured"][name],
                                         getattr(reviewed, name), notes)
                          for name in AxisSeconds._fields})
    if runner_overhead_seconds is None:
        notes.append("worker_setup_seconds: a worker's log cannot see runner boot, artifact "
                     "download or pip install, so this needs --runner-overhead-seconds; "
                     f"keeping the reviewed {reviewed_setup:g}")
        setup = reviewed_setup
    else:
        setup = _clamped("worker_setup_seconds",
                         diagnostics["measured"]["worker_setup_seconds"], reviewed_setup, notes)
    runner = fit_runner_profile(rows)
    diagnostics["runner"] = runner._asdict()
    return Calibration(axis, setup, density_exponent, runner, diagnostics, notes)


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
        raise ValueError(f"no {', '.join(missing)} in the calibration; it must carry all "
                         f"{len(AxisSeconds._fields)} coefficients")
    return AxisSeconds(**{name: float(axis[name]) for name in AxisSeconds._fields})


def load_calibration_file(path):
    """Read a calibration file.

    Args:
        path: The file to read.

    Returns:
        Its per-axis seconds, and its runner profile.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If it is not valid JSON, or is missing a coefficient.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return axis_seconds_from_json(data), runner_profile_from_json(data)
