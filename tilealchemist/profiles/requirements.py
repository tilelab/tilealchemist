"""A profile's PEP 723 dependencies, read without importing it.

See docs/reference/profile-api.md.
"""
import re
import tomllib
from typing import Any, TextIO

# PEP 723's reference regex, verbatim.
BLOCK_PATTERN: str = (r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s"
                      r"(?P<content>(^#(| .*)$\s)+)^# ///$")

BLOCK_TYPE: str = "script"

# The reference regex merges two adjacent blocks, so openers are counted.
OPENER_PATTERN: str = rf"(?m)^# /// {BLOCK_TYPE}$"

# The reference regex needs a content line, so it misses an empty block.
EMPTY_BLOCK_PATTERN: str = rf"(?m)^# /// {BLOCK_TYPE}$\s^# ///$"


def read_metadata(source: str) -> dict[str, Any]:
    """Returns the parsed `script` block of `source`, empty if it has none.

    Raises:
        ValueError: If there are several blocks or one is never closed.
    """
    openers: int = len(re.findall(OPENER_PATTERN, source))
    if openers > 1:
        raise ValueError(f"{openers} `# /// {BLOCK_TYPE}` blocks found; "
                         f"PEP 723 allows one")
    block: re.Match[str] | None = next(
        (match for match in re.finditer(BLOCK_PATTERN, source)
         if match.group("type") == BLOCK_TYPE), None)
    if block is None:
        if openers and not re.search(EMPTY_BLOCK_PATTERN, source):
            raise ValueError(
                f"`# /// {BLOCK_TYPE}` block has no closing `# ///`")
        return {}
    # Strip "# " from content lines and "#" from blank ones.
    lines: list[str] = block.group("content").splitlines(keepends=True)
    content: str = "".join(line[2:] if line.startswith("# ") else line[1:]
                           for line in lines)
    return tomllib.loads(content)


def profile_requirements(path: str) -> list[str]:
    """Returns the PEP 508 requirements the profile at `path` declares.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the block is malformed or `dependencies` is not a list
            of strings.
    """
    profile_file: TextIO
    with open(path, encoding="utf-8") as profile_file:
        metadata: dict[str, Any] = read_metadata(profile_file.read())
    requirements: Any = metadata.get("dependencies", [])
    if not isinstance(requirements, list) or not all(
            isinstance(requirement, str) for requirement in requirements):
        raise ValueError(
            "`dependencies` must be an array of requirement strings")
    return requirements
