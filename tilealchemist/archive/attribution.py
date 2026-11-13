"""What a built layer credits; see docs/reference/run-config.md."""
from typing import Any

PLACEHOLDER: str = "{source}"


def declared_attribution(metadata: Any) -> str | None:
    """Returns the attribution in an archive's metadata, if not blank.

    Args:
        metadata: The archive's PMTiles metadata, as parsed JSON.

    Raises:
        ValueError: If the metadata is not a JSON object or its
            `attribution` is not a string.
    """
    if not isinstance(metadata, dict):
        raise ValueError(f"the archive's PMTiles metadata is a "
                         f"{type(metadata).__name__}, not a JSON object")
    attribution: Any = metadata.get("attribution")
    if attribution is None:
        return None
    if not isinstance(attribution, str):
        raise ValueError(f"the archive's `attribution` is a "
                         f"{type(attribution).__name__}, not a string")
    return attribution.strip() or None


def compose_attribution(declared: str | None, template: str | None) -> str:
    """Returns what the layer credits.

    Args:
        declared: What the archive declares, or None.
        template: The config's `attribution`; `{source}` in it is replaced by
            `declared`. None, empty or blank means `declared` as it is.

    Raises:
        ValueError: If that leaves nothing, or `{source}` has nothing to
            stand for.
    """
    if not template or template.isspace():
        if not declared:
            raise ValueError(
                "the source archive declares no attribution of its own (no "
                "`attribution` key in its PMTiles metadata), so there is "
                "nothing to carry into the output; state it with the "
                "config's `attribution`")
        return declared

    if PLACEHOLDER in template and not declared:
        raise ValueError(
            f"the `attribution` template has a {PLACEHOLDER} to fill in, "
            f"but the source archive declares no attribution of its own; "
            f"state the whole attribution "
            f"instead, without {PLACEHOLDER}")

    return template.replace(PLACEHOLDER, declared or "").strip()
