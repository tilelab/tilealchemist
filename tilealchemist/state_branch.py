"""The axis state on its own branch, through the GitHub contents API."""
import base64
import json
import sys
import time

import requests

from tilealchemist.backoff import backoff_delay

API_ROOT = "https://api.github.com"

BASE_DELAY_SECONDS = 2.0

MAX_ATTEMPTS = 8

# A git constant: the content hash of zero tree entries, the same in every repository.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_BRANCH = "state"

# Past 1 MB the contents API answers only these media types, with the content left out.
OBJECT_MEDIA_TYPE = "application/vnd.github.object+json"


def _headers(token, accept=None):
    """The headers every call to the API carries.

    Args:
        token: The token to authenticate with.
        accept: The media type to ask for, the API's own JSON by default.

    Returns:
        The headers, pinning the API version so a server-side default cannot
        change the shape of a reply underneath us.
    """
    return {"Accept": accept or "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"}


def _retryable(response):
    """Whether this reply is worth trying again.

    Args:
        response: The reply to judge.

    Returns:
        True for a server error or a secondary rate limit. A 403 carrying no
        rate-limit signal is a permissions problem, which retrying cannot fix.
    """
    if response.status_code >= 500:
        return True
    if response.status_code != 403:
        return False
    return "rate limit" in response.text.lower() or "abuse" in response.text.lower()


def request(method, path, token, accept=None, **kwargs):
    """Call the API once, retrying what is worth retrying.

    Args:
        method: The HTTP method.
        path: The path under the API root, starting with a slash.
        token: The token to authenticate with.
        accept: The media type to ask for, the API's own JSON by default.
        **kwargs: Passed through to requests, for the JSON body.

    Returns:
        The last response received, whose status the caller judges.
    """
    response = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = requests.request(method, f"{API_ROOT}{path}",
                                     headers=_headers(token, accept),
                                     timeout=60, **kwargs)
        if not _retryable(response) or attempt == MAX_ATTEMPTS:
            return response
        delay = backoff_delay(attempt, response, BASE_DELAY_SECONDS)
        print(f"{method} {path}: {response.status_code}, retrying in {delay:.1f}s",
              file=sys.stderr)
        time.sleep(delay)
    return response


def ref_sha(repo, token, ref):
    """The commit a ref points at.

    Args:
        repo: The `owner/name` the state lives in.
        token: The token to authenticate with.
        ref: The ref without its leading `refs/`, such as `heads/state`.

    Returns:
        The sha, or None where the ref does not exist.
    """
    response = request("GET", f"/repos/{repo}/git/ref/{ref}", token)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["object"]["sha"]


def ensure_branch(repo, token, branch=DEFAULT_BRANCH):
    """Create the state branch as an empty orphan if it is not there yet.

    The branch carries no code and shares no history with the default branch,
    so a state write can never touch what the run was built from, and nobody
    has to set the branch up by hand before the first run.

    Args:
        repo: The `owner/name` the state lives in.
        token: The token to authenticate with.
        branch: The branch to ensure.

    Returns:
        The branch tip's sha.
    """
    sha = ref_sha(repo, token, f"heads/{branch}")
    if sha is not None:
        return sha
    response = request("POST", f"/repos/{repo}/git/commits", token,
                        json={"message": f"init {branch} branch", "tree": EMPTY_TREE_SHA,
                              "parents": []})
    response.raise_for_status()
    commit_sha = response.json()["sha"]
    created = request("POST", f"/repos/{repo}/git/refs", token,
                       json={"ref": f"refs/heads/{branch}", "sha": commit_sha})
    if created.status_code == 201:
        return commit_sha
    # 422 means another job created the branch between the read and this write.
    if created.status_code != 422:
        created.raise_for_status()
    return ref_sha(repo, token, f"heads/{branch}")


def read_json(repo, token, branch, path):
    """Read one JSON file off the state branch.

    Args:
        repo: The `owner/name` the state lives in.
        token: The token to authenticate with.
        branch: The branch to read.
        path: The file's path inside the branch.

    Returns:
        Its parsed contents and the blob sha to write back against. A missing
        file reads as an empty mapping with no sha, which is what lets the
        first run write without a special case. A file past the contents
        API's 1 MB is read through the git blob API instead, which the block
        state outgrows.

    Raises:
        ValueError: If the file exists but does not hold valid JSON, which
            must not be silently replaced with an empty document.
    """
    response = request("GET", f"/repos/{repo}/contents/{path}", token,
                        accept=OBJECT_MEDIA_TYPE, params={"ref": branch})
    if response.status_code == 404:
        return {}, None
    response.raise_for_status()
    document = response.json()
    content = document.get("content")
    if document.get("encoding") == "none":
        blob = request("GET", f"/repos/{repo}/git/blobs/{document['sha']}", token)
        blob.raise_for_status()
        content = blob.json()["content"]
    try:
        return json.loads(base64.b64decode(content)), document["sha"]
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} on {branch} is not valid JSON: {error}") from error


def write_json(repo, token, branch, path, content, sha, message, compact=False):
    """Write one JSON file to the state branch.

    Args:
        repo: The `owner/name` the state lives in.
        token: The token to authenticate with.
        branch: The branch to write.
        path: The file's path inside the branch.
        content: The document to write.
        sha: The blob sha the write is made against, None for a new file.
        message: The commit message.
        compact: Whether to leave out the indentation, for a document too
            large for it to be worth reading by eye.

    Returns:
        True where the write landed, and False where the file moved underneath
        it, which the caller answers by reading again and reapplying.
    """
    text = (json.dumps(content, separators=(",", ":"), sort_keys=True) if compact
            else json.dumps(content, indent=2, sort_keys=True))
    body = {"message": message, "branch": branch,
            "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    response = request("PUT", f"/repos/{repo}/contents/{path}", token, json=body)
    if response.status_code in (200, 201):
        return True
    if response.status_code in (409, 422):
        return False
    response.raise_for_status()
    return False


def update_json(repo, token, branch, path, mutate, message, max_attempts=MAX_ATTEMPTS,
                compact=False):
    """Apply a change to one JSON file, retrying if it moved underneath us.

    Read, change, write against the sha that was read: the write is refused
    rather than overwriting a document another job wrote in between, and the
    whole change is then reapplied to the newer one.

    Args:
        repo: The `owner/name` the state lives in.
        token: The token to authenticate with.
        branch: The branch to write.
        path: The file's path inside the branch.
        mutate: Called with the parsed document, returning what to write.
        message: The commit message.
        max_attempts: How many times to reapply before giving up.
        compact: Whether to write the document without indentation.

    Returns:
        The document as written.

    Raises:
        RuntimeError: If the file kept moving for every attempt.
    """
    for attempt in range(1, max_attempts + 1):
        document, sha = read_json(repo, token, branch, path)
        updated = mutate(document)
        if write_json(repo, token, branch, path, updated, sha, message, compact=compact):
            return updated
        print(f"{path} moved underneath attempt {attempt}, reapplying", file=sys.stderr)
    raise RuntimeError(f"{path} on {branch} kept moving; gave up after {max_attempts} attempts")
