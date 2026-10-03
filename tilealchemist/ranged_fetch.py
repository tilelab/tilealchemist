"""HTTP Range fetching against the source PMTiles archive."""
import sys
import time

import requests

from tilealchemist.backoff import backoff_delay
from tilealchemist.throttle import UpdateLineThrottle

# Covers the transient CDN failures of a cold-cache stampede; see
# docs/ARCHITECTURE.md.
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
        percent = ((100 * downloaded / self.total_bytes)
                   if self.total_bytes else 100.0)
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


def _attempt_fetch_range(session, url, range_header, on_chunk, chunk_size,
                         dest=None):
    """Make one ranged request and read its body.

    Args:
        session: The requests session to use.
        url: Absolute URL of the archive.
        range_header: The `bytes=start-end` value to ask for.
        on_chunk: Called with the running byte total as chunks arrive, or None.
        chunk_size: How many bytes to read at a time.
        dest: An open binary file to stream the body into, or None to return
            it as bytes. A caller that passes one rewinds it before a retry.

    Returns:
        The range's bytes, or the number of bytes written when `dest` is given.

    Raises:
        _RetryableFailure: On a retryable status, on any status other than
            206, or if the connection drops part-way through the body.
        requests.HTTPError: On a status not worth retrying.
    """
    # Leaving the `with` on a raise returns the connection before the caller's
    # backoff sleeps.
    with session.get(url, headers={"Range": range_header}, timeout=READ_TIMEOUT,
                     stream=True) as response:
        status = response.status_code

        if status in RETRYABLE_STATUS_CODES:
            raise _RetryableFailure(
                f"got HTTP {status} for range {range_header}", response,
                requests.HTTPError(f"HTTP {status} ({response.reason}) for "
                                   f"range {range_header} of {url}",
                                   response=response))

        response.raise_for_status()
        if status != 206:
            raise _RetryableFailure(
                f"got HTTP {status} instead of 206 for range {range_header}",
                response,
                RuntimeError(
                    f"expected HTTP 206 Partial Content for ranged request "
                    f"({range_header}) after {MAX_RANGE_ATTEMPTS} attempts, "
                    f"got {status}: server ignored the Range header and would "
                    f"send the entire archive instead of just this range"))

        # A bytearray, not a list to join: a join holds the body twice.
        body = bytearray() if dest is None else None
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                if dest is None:
                    body += chunk
                else:
                    dest.write(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(downloaded)
            return downloaded if dest is not None else bytes(body)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as error:
            # No response left to read a Retry-After from, so the backoff runs
            # on jitter alone.
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
                on_chunk=None, chunk_size=1024 * 1024, dest_path=None):
    """Fetch a byte range, retrying the failures that are worth retrying.

    Args:
        session: The requests session to use.
        url: Absolute URL of the archive.
        offset: First byte to read.
        length: How many bytes to read.
        retry_label: What to call this fetch in a retry warning.
        on_chunk: Called with the running byte total as chunks arrive, or None.
        chunk_size: How many bytes to read at a time.
        dest_path: A path to stream the range into rather than holding it in
            memory. The file is truncated before every attempt, a partial one
            being no use to a caller that has to ask again from byte zero.

    Returns:
        The range's bytes, or the number of bytes written when `dest_path` is
        given.

    Raises:
        requests.HTTPError: If the server kept failing, or failed in a way not
            worth retrying.
        RuntimeError: If the server ignored the Range header, and so would
            send the whole archive rather than this range.
    """
    range_header = f"bytes={offset}-{offset + length - 1}"
    if dest_path is not None:
        with open(dest_path, "wb") as dest:
            return _fetch_range_attempts(session, url, range_header,
                                         on_chunk, chunk_size, retry_label,
                                         dest)
    return _fetch_range_attempts(session, url, range_header, on_chunk,
                                 chunk_size, retry_label, None)


def _fetch_range_attempts(session, url, range_header, on_chunk, chunk_size,
                          retry_label, dest):
    """Run one range's attempts until one succeeds or they run out.

    Args:
        session: The requests session to use.
        url: Absolute URL of the archive.
        range_header: The `bytes=start-end` value to ask for.
        on_chunk: Called with the running byte total as chunks arrive, or None.
        chunk_size: How many bytes to read at a time.
        retry_label: What to call this fetch in a retry warning.
        dest: An open binary file to stream into, or None to return bytes.

    Returns:
        The range's bytes, or the number of bytes written when `dest` is given.

    Raises:
        requests.HTTPError: If the server kept failing, or failed in a way not
            worth retrying.
        RuntimeError: If the server ignored the Range header.
    """
    for attempt in range(1, MAX_RANGE_ATTEMPTS + 1):
        try:
            if dest is not None:
                # Every attempt asks from byte zero, so a partial write must go.
                dest.seek(0)
                dest.truncate(0)
            return _attempt_fetch_range(session, url, range_header,
                                        on_chunk, chunk_size, dest)
        except _RetryableFailure as failure:
            if attempt == MAX_RANGE_ATTEMPTS:
                raise failure.final from failure
            delay = backoff_delay(attempt, failure.response,
                                  RANGE_RETRY_BASE_DELAY)
            _warn_retry(retry_label, failure.detail, attempt, delay)
            time.sleep(delay)
