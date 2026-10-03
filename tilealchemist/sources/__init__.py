"""Source registry: CLI-facing `--source` name -> `Source` class."""
from tilealchemist.schemas import SCHEMAS
from tilealchemist.sources.openfreemap import OpenFreeMapSource
from tilealchemist.sources.protomaps import ProtomapsSource
from tilealchemist.sources.static_url import StaticUrlSource

SOURCES = {
    "openfreemap": OpenFreeMapSource,
    "protomaps": ProtomapsSource,
    "static-url": StaticUrlSource,
}


def resolve_source(source_name, source_url, schema=None):
    """Build the `Source` these CLI arguments name, not yet resolved.

    Args:
        source_name: The `--source` value, a key of SOURCES.
        source_url: The `--source-url` value, or None.
        schema: The `--schema` value as a SchemaName, or None.

    Returns:
        The named Source.

    Raises:
        ValueError: If static-url is missing its URL or schema, or another
            source is given a schema it does not publish.
    """
    if source_name == "static-url":
        if not source_url:
            raise ValueError(
                "--source-url is required when --source static-url")
        if not schema:
            raise ValueError("--schema is required when --source static-url: "
                             "a bare URL could be any provider's archive, so "
                             "nothing else can say what schema its tiles are "
                             "in")
        return StaticUrlSource(source_url, SCHEMAS[schema])

    source = SOURCES[source_name]()
    # A contradicting --schema is refused rather than ignored.
    if schema is not None and schema != source.schema.name:
        raise ValueError(f"--source {source_name} publishes "
                         f"{source.schema.name} tiles, so --schema {schema} "
                         f"cannot be right; leave --schema off (only --source "
                         f"static-url needs one)")
    return source
