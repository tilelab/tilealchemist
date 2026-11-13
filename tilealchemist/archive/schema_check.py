"""Whether an archive is in a schema; see docs/reference/schemas.md."""
from typing import Any

from tilealchemist.config import plugins
from tilealchemist.console import log
from tilealchemist.schemas.base import TileSchema


def check_schema(schema: TileSchema, metadata: Any, archive: str) -> None:
    """Checks that an archive's metadata lists the schema's archive_layers.

    Args:
        schema: The schema to read the archive with.
        metadata: The archive's metadata, as parsed JSON.
        archive: The archive, for messages.

    Raises:
        ValueError: If it lacks one, naming the schemas it would fit.
    """
    vector_layers: Any = metadata.get("vector_layers")
    if not isinstance(vector_layers, list):
        log.warning("schema", f"{archive} lists no vector_layers in its "
                    f"metadata, so it is read as {schema.name} unchecked")
        return
    listed: set[str] = {layer["id"] for layer in vector_layers}
    missing: list[str] = sorted(schema.archive_layers - listed)
    if not missing:
        return
    fitting: list[str] = [
        name for name in plugins.schema_names()
        if plugins.schema(name).archive_layers <= listed]
    raise ValueError(
        f"{archive} has no {', '.join(missing)} layer, so it isn't in "
        f"schema {schema.name}; set the config's `schema` to "
        f"{' or '.join(repr(name) for name in fitting) or 'one that fits'}")
