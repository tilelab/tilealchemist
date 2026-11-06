"""Plugins found through entry points: the lookup, and installed schemas.

This package declares its own the same way plugins do, in pyproject.toml.
Each is loaded only when asked for. See docs/reference/schemas.md.
"""
import functools
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from tilealchemist.schemas.base import TileSchema

SCHEMA_GROUP: str = "tilealchemist.schemas"


@functools.cache
def _entry_points(group: str) -> dict[str, EntryPoint]:
    """Returns the group's entry points by name.

    Raises:
        ValueError: If two packages declare the same name.
    """
    found: dict[str, EntryPoint] = {}
    entry_point: EntryPoint
    for entry_point in entry_points(group=group):
        if entry_point.name in found:
            raise ValueError(
                f"{group} entry point {entry_point.name!r} is declared by "
                f"both {_owner(found[entry_point.name])} and "
                f"{_owner(entry_point)}; uninstall one of them")
        found[entry_point.name] = entry_point
    return found


def _owner(entry_point: EntryPoint) -> str:
    """Returns the entry point's distribution name, or else its target."""
    return entry_point.dist.name if entry_point.dist else entry_point.value


def names(group: str) -> list[str]:
    """Returns the names installed in an entry-point group, sorted."""
    return sorted(_entry_points(group))


def load(group: str, kind: str, name: str) -> Any:
    """Returns what the entry point `name` in `group` points at.

    Args:
        group: The entry-point group.
        kind: `source`, `schema` or `command`, for the error.
        name: The name asked for.

    Raises:
        ValueError: If nothing installed declares `name`.
    """
    found: dict[str, EntryPoint] = _entry_points(group)
    if name not in found:
        installed: str = ", ".join(sorted(found)) or "none at all"
        hint: str = ("" if found else "; tilealchemist's own are declared in "
                     "its package metadata, so it has to be installed, not "
                     "only importable")
        raise ValueError(
            f"no {kind} named {name!r} is installed (installed: "
            f"{installed}){hint}. A plugin providing it has to be installed "
            f"wherever the run's steps run; see docs/reference/schemas.md")
    return found[name].load()


def schema_names() -> list[str]:
    """Returns the installed schemas' names, sorted."""
    return names(SCHEMA_GROUP)


@functools.cache
def schema(name: str) -> TileSchema:
    """Returns the TileSchema installed as `name`.

    Raises:
        ValueError: If no such schema is installed, or its `name` differs
            from the entry point's, which configs rely on.
    """
    loaded: TileSchema = load(SCHEMA_GROUP, "schema", name)
    if loaded.name != name:
        raise ValueError(f"the schema installed as {name!r} calls itself "
                         f"{loaded.name!r}; its entry point and its `name` "
                         f"must agree")
    return loaded
