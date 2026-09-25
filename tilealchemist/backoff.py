"""Retry delay for a throttled or failed ranged request."""
import random


def _server_requested_delay(response):
    """Read the delay the server itself asked for.

    Args:
        response: The response that failed or was throttled, or None when the
            attempt raised before any response arrived.

    Returns:
        The Retry-After value in seconds, or None when the server sent no
        usable one.
    """
    if response is None:
        return None
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _jittered_backoff(attempt, base_delay):
    """Grow the delay exponentially, with jitter.

    Args:
        attempt: 1 for the first retry, 2 for the second, and so on.
        base_delay: The delay the first retry starts from, in seconds.

    Returns:
        The delay to wait, in seconds.
    """
    # Jitter breaks the lockstep that would reproduce the throttling burst.
    return base_delay * (2 ** (attempt - 1)) * random.uniform(1.0, 1.5)


def backoff_delay(attempt, response, base_delay):
    """Decide how long to wait before retrying a ranged request.

    A Retry-After the server sent wins. Failing that the delay doubles per
    attempt, jittered so that workers throttled together do not come back in
    lockstep and reproduce the burst.

    Args:
        attempt: 1 for the first retry, 2 for the second, and so on.
        response: The response that failed, or None if none arrived.
        base_delay: The delay the first retry starts from, in seconds.

    Returns:
        The delay to wait, in seconds.
    """
    return _server_requested_delay(response) or _jittered_backoff(attempt, base_delay)
