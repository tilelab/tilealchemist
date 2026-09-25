"""HTTP Range fetching against the source PMTiles archive."""
import sys
import time

import requests

from tilealchemist.backoff import backoff_delay
from tilealchemist.throttle import UpdateLineThrottle

# Covers the transient CDN failures of a cold-cache stampede; see docs/ARCHITECTURE.md.
MAX_RANGE_ATTEMPTS = 6
RANGE_RETRY_BASE_DELAY = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

READ_TIMEOUT = (10, 60)


def make_session():
    """Build the session every ranged fetch in a run shares.

    Returns:
        A requests session whose HTTPS adapter retries a little on its own,
        underneath the coarser retry loop in fetch_range().
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("https://", adapter)
    return session


class DownloadProgress:
    """Prints how far a download has got, at most once per interval.

    Attributes:
        total_bytes: How many bytes the download is expected to move.
        label: What to call this download in the line.
        throttle: The rate limiter the lines go through.
    """

    def __init__(self, total_bytes, interval, label):
        """Set up progress reporting for one download.

        Args:
            total_bytes: How many bytes the download is expected to move.
            interval: Minimum seconds between two lines.
            label: What to call this download in the line.
        """
        self.total_bytes = total_bytes
        self.label = label
        self.throttle = UpdateLineThrottle(interval, fire_immediately=True)

    def update(self, downloaded):
        """Report progress, if a line is due.

        Args:
            downloaded: The running total for the current attempt, not for the
                download as a whole, so that a retry rewinds the figure rather
                than doubling it.
        """
        if not self.throttle.due():
            return
        percent = (100 * downloaded / self.total_bytes) if self.total_bytes else 100.0
        print(f"update: downloading {self.label}: "
              f"{downloaded}/{self.total_bytes} bytes ({percent:.1f}%)",
              file=sys.stderr)


class _RetryableFailure(Exception):
    """A fetch attempt that failed in a way worth trying again.

    Attributes:
        detail: One line saying what went wrong, for the retry warning.
        response: The response it failed on, or None if none arrived.
        final: The exception to raise once the attempts run out.
    """

    def __init__(self, detail, response, final):
        """Record a failed attempt.

        Args:
            detail: One line saying what went wrong.
            response: The response it failed on, or None if none arrived.
            final: The exception to raise once the attempts run out.
        """
        super().__init__(detail)
        self.detail = detail
        self.response = response
        self.final = final


def _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size):
    """Make one ranged request and read its body.

    Args:
        session: The requests session to use.
        url: Absolute URL of the archive.
        range_header: The `bytes=start-end` value to ask for.
        on_chunk: Called with the running byte total as chunks arrive, or None.
        chunk_size: How many bytes to read at a time.

    Returns:
        The range's bytes.

    Raises:
        _RetryableFailure: On a retryable status, on any status other than
            206, or if the connection drops part-way through the body.
        requests.HTTPError: On a status not worth retrying.
    """
    # Leaving the `with` on a raise returns the connection before the caller's backoff sleeps.
    with session.get(url, headers={"Range": range_header}, timeout=READ_TIMEOUT,
                     stream=True) as response:
        status = response.status_code

        if status in RETRYABLE_STATUS_CODES:
            raise _RetryableFailure(
                f"got HTTP {status} for range {range_header}", response,
                requests.HTTPError(f"HTTP {status} ({response.reason}) for range "
                                   f"{range_header} of {url}", response=response))

        response.raise_for_status()
        if status != 206:
            raise _RetryableFailure(
                f"got HTTP {status} instead of 206 for range {range_header}", response,
                RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request ({range_header}) "
                    f"after {MAX_RANGE_ATTEMPTS} attempts, got {status}: server ignored the "
                    f"Range header and would send the entire "
                    f"archive instead of just this range"))

        chunks = []
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                chunks.append(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(downloaded)
            return b"".join(chunks)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as error:
            # No response left to read a Retry-After from, so the backoff runs on jitter alone.
            raise _RetryableFailure(
                f"connection dropped after {downloaded} bytes "
                f"({error.__class__.__name__})", None, error) from error


def _warn_retry(retry_label, detail, attempt, delay):
    """Warn that an attempt failed and another is coming.

    Args:
        retry_label: What to call the failing fetch in the warning title.
        detail: One line saying what went wrong.
        attempt: Which attempt has just failed, counting from one.
        delay: Seconds before the next attempt.
    """
    print(f"::warning title={retry_label} retry::{detail} "
          f"(attempt {attempt}/{MAX_RANGE_ATTEMPTS}), retrying in {delay:.0f}s",
          file=sys.stderr)


def fetch_range(session, url, offset, length, retry_label,
                on_chunk=None, chunk_size=1024 * 1024):
    """Fetch a byte range, retrying the failures that are worth retrying.

    Args:
        session: The requests session to use.
        url: Absolute URL of the archive.
        offset: First byte to read.
        length: How many bytes to read.
        retry_label: What to call this fetch in a retry warning.
        on_chunk: Called with the running byte total as chunks arrive, or None.
        chunk_size: How many bytes to read at a time.

    Returns:
        The range's bytes.

    Raises:
        requests.HTTPError: If the server kept failing, or failed in a way not
            worth retrying.
        RuntimeError: If the server ignored the Range header, and so would
            send the whole archive rather than this range.
    """
    range_header = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        try:
            return _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size)
        except _RetryableFailure as failure:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise failure.final from failure
            delay = backoff_delay(attempt, failure.response, RANGE_RETRY_BASE_DELAY)
            _warn_retry(retry_label, failure.detail, attempt, delay)
            time.sleep(delay)
