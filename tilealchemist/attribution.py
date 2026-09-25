"""What a built layer credits; see docs/ARCHITECTURE.md "Source attribution"."""
import gzip
import json

from tilealchemist.ranged_fetch import fetch_range

RETRY_LABEL = "prepare-shards metadata"
PLACEHOLDER = "{source}"


def fetch_declared_attribution(session, url, header):
    """Read the attribution the archive states for itself.

    Args:
        session: The requests session the ranged fetches share.
        url: Absolute URL of the source archive.
        header: The archive's parsed PMTiles header.

    Returns:
        The declared attribution, stripped, or None where the archive carries
        no metadata, no `attribution` key, or a blank one.
    """
    length = header["metadata_length"]
    if length == 0:
        return None
    blob = fetch_range(session, url, header["metadata_offset"], length,
                       retry_label=RETRY_LABEL)
    # gzip, not header["internal_compression"]: the directory walk already rejected the rest.
    document = json.loads(gzip.decompress(blob))
    attribution = document.get("attribution") if isinstance(document, dict) else None
    if not isinstance(attribution, str) or not attribution.strip():
        return None
    return attribution.strip()


def compose_attribution(declared, template):
    """Decide what the built layer credits.

    Args:
        declared: What the source archive states for itself, or None.
        template: The pipeline's `attribution` input, whose `{source}` is
            filled in with `declared`. Empty carries `declared` through
            unchanged.

    Returns:
        The attribution the layer will carry.

    Raises:
        ValueError: If there is nothing to carry, if the template has a
            `{source}` the archive gives nothing to fill it with, or if the
            template composes to nothing at all.
    """
    if not template:
        if not declared:
            raise ValueError(
                "the source archive declares no attribution of its own (no `attribution` "
                "key in its PMTiles metadata), so there is nothing to carry into the "
                "output; state it with the pipeline's `attribution` input")
        return declared

    if PLACEHOLDER in template and not declared:
        raise ValueError(
            f"the `attribution` template has a {PLACEHOLDER} to fill in, but the source "
            f"archive declares no attribution of its own; state the whole attribution "
            f"instead, without {PLACEHOLDER}")

    # Literal substitution, never the workflow shell's: `${v//p/r}` mangles `&copy;`.
    attribution = template.replace(PLACEHOLDER, declared or "").strip()
    if not attribution:
        raise ValueError(f"the `attribution` template {template!r} composes to nothing; a "
                         f"layer MUST carry an attribution")
    return attribution
