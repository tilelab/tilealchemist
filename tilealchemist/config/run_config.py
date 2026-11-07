"""What a run builds: its TOML config.

A config names the profiles (one layer each, named after the profile's
`name`), the schema, the zoom range, the attribution and the plugins. Paths
in it are relative to the file. The archive a run reads, how it is split
and where it runs are command-line flags, never the config's.

    schema = "protomaps"
    max_zoom = 14
    attribution = '<a href="https://example.org">Example</a> {source}'
    profiles = ["land.py", "cropped_waterways.py"]

--min-zoom and --max-zoom override its zoom range. See
docs/reference/run-config.md.
"""
import dataclasses
import os
import re
import tomllib
from collections.abc import Sequence
from typing import Any, BinaryIO

from tilealchemist.archive.zoom import MAX_SUPPORTED_ZOOM, ZoomLevel
from tilealchemist.config import plugins
from tilealchemist.profiles.base import Profile

CONFIG_SUFFIX: str = ".toml"

# A layer's name is its file name, `<name>.pmtiles`.
LAYER_NAME: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# How pip tells a local directory from a package to fetch.
PATH_PREFIXES: tuple[str, ...] = (".", "/", "~")

# The keys a config may set, grouped by how they are read.
_STRING_KEYS: tuple[str, ...] = ("schema", "attribution")
_ZOOM_KEYS: tuple[str, ...] = ("min_zoom", "max_zoom")
_CONFIG_KEYS: tuple[str, ...] = ("profiles", *_STRING_KEYS, *_ZOOM_KEYS,
                                 "plugins")


@dataclasses.dataclass(frozen=True)
class ConvertConfig:
    """What a run builds.

    Attributes:
        profiles: Each layer's profile file, absolute, in order.
        schema: The schema the archive's tiles are in; None if the config
            names none, which check() refuses.
        min_zoom: Lowest zoom level to build.
        max_zoom: Highest zoom level to build.
        attribution: What the layers credit, `{source}` standing for the
            archive's own; None for the archive's alone.
        plugins: Pip requirements every step needs; paths are absolute.
    """

    profiles: tuple[str, ...]
    schema: str | None = None
    min_zoom: ZoomLevel = ZoomLevel.Z0
    max_zoom: ZoomLevel = ZoomLevel.Z14
    attribution: str | None = None
    plugins: tuple[str, ...] = ()


def read_config(path: str) -> ConvertConfig:
    """Returns the config file at `path`, its paths made absolute.

    Raises:
        ValueError: If it is no .toml file, a key is unknown, a value has the
            wrong type, there are no profiles, or a named file is missing.
        OSError: If the file cannot be read.
    """
    if not path.endswith(CONFIG_SUFFIX):
        raise ValueError(f"{path} is no {CONFIG_SUFFIX} config; a config "
                         f"lists the profiles to build in `profiles`")
    handle: BinaryIO
    with open(path, "rb") as handle:
        try:
            document: dict[str, Any] = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"{path}: {error}") from None
    base: str = os.path.dirname(os.path.abspath(path))
    unknown: list[str] = sorted(set(document) - set(_CONFIG_KEYS))
    if unknown:
        raise ValueError(f"{path}: unknown key {unknown[0]!r}; a config sets "
                         f"{', '.join(_CONFIG_KEYS)}")
    profiles: tuple[str, ...] = _read_profiles(path, base,
                                               document.get("profiles"))
    values: dict[str, Any] = {}
    key: str
    for key in _STRING_KEYS:
        if key in document:
            values[key] = _string(path, key, document[key])
    for key in _ZOOM_KEYS:
        if key in document:
            values[key] = zoom_level(document[key], f"{path}: {key}")
    plugin_specs: Any = document.get("plugins", [])
    if not isinstance(plugin_specs, list):
        raise ValueError(f"{path}: plugins must be an array of pip "
                         f"requirements")
    values["plugins"] = tuple(
        _resolve_plugin(_string(path, "plugins", spec), base)
        for spec in plugin_specs)
    return ConvertConfig(profiles=profiles, **values)


def _read_profiles(path: str, base: str, specs: Any) -> tuple[str, ...]:
    """Returns the config's profiles as absolute paths.

    Args:
        path: The config, for errors.
        base: The directory the paths are relative to.
        specs: The parsed `profiles`, or None.

    Raises:
        ValueError: If there are none, or one is no string or no file.
    """
    if not isinstance(specs, list) or not specs:
        raise ValueError(f"{path}: profiles must be a non-empty array of "
                         f"profile files; a config builds at least one layer")
    profiles: list[str] = []
    spec: Any
    for spec in specs:
        profile: str = os.path.join(base, _string(path, "profiles", spec))
        _check_file(profile, f"{path}: profile")
        profiles.append(profile)
    return tuple(profiles)


def with_overrides(config: ConvertConfig, **overrides: Any) -> ConvertConfig:
    """Returns `config` with the given fields replaced, except None ones."""
    return dataclasses.replace(
        config, **{key: value for key, value in overrides.items()
                   if value is not None})


def check(config: ConvertConfig) -> None:
    """Checks that the config's values fit together.

    Raises:
        ValueError: If the zoom range is reversed, or the schema is missing
            or not installed.
    """
    if config.min_zoom > config.max_zoom:
        raise ValueError(f"min_zoom ({config.min_zoom}) must not exceed "
                         f"max_zoom ({config.max_zoom})")
    schema: str = schema_name(config)
    schema_names: list[str] = plugins.schema_names()
    if schema not in schema_names:
        raise ValueError(f"schema {schema!r} is not installed "
                         f"(installed: {', '.join(schema_names)}); list the "
                         f"plugin providing it in the config's `plugins` "
                         f"and run `tilealchemist setup`")


def schema_name(config: ConvertConfig) -> str:
    """Returns the schema the config names.

    Raises:
        ValueError: If it names none.
    """
    if config.schema is None:
        raise ValueError(f"the config names no schema; set `schema` to the "
                         f"archive's, one of "
                         f"{', '.join(plugins.schema_names())}")
    return config.schema


def check_layer_name(name: str, where: str) -> None:
    """Checks that a layer name is a plain file name.

    Raises:
        ValueError: If it isn't, naming `where` it came from.
    """
    if not LAYER_NAME.fullmatch(name):
        raise ValueError(f"{where}: layer name {name!r} must be letters, "
                         f"digits, '.', '_' and '-', starting with a letter "
                         f"or digit")


def layer_names(paths: Sequence[str],
                profiles: Sequence[Profile]) -> list[str]:
    """Returns each profile's name as its layer's name.

    Raises:
        ValueError: If a name is no valid file name, or two are the same.
    """
    names: list[str] = []
    path: str
    profile: Profile
    for path, profile in zip(paths, profiles, strict=True):
        check_layer_name(profile.name, path)
        if profile.name in names:
            raise ValueError(f"{path}: another profile is named "
                             f"{profile.name!r} too; every layer is named "
                             f"after its profile, so each needs a name of "
                             f"its own")
        names.append(profile.name)
    return names


def zoom_level(value: Any, where: str) -> ZoomLevel:
    """Returns `value`, an int or numeric string, as a ZoomLevel.

    Raises:
        ValueError: If it is no whole number in range, naming `where`.
    """
    # bool is an int, and int() would floor a float instead of refusing it.
    if isinstance(value, int | str) and not isinstance(value, bool):
        try:
            return ZoomLevel(int(value))
        except ValueError:
            pass
    raise ValueError(f"{where}: {value!r} is not a zoom level between 0 "
                     f"and {MAX_SUPPORTED_ZOOM}")


def is_path_requirement(spec: str) -> bool:
    """Returns whether a pip requirement is a local directory."""
    return spec.startswith(PATH_PREFIXES)


def _resolve_plugin(spec: str, base: str) -> str:
    """Returns a plugin directory made absolute against `base`.

    Other requirements are returned unchanged.

    Raises:
        ValueError: If the directory doesn't exist.
    """
    if not is_path_requirement(spec):
        return spec
    path: str = os.path.normpath(os.path.join(base, os.path.expanduser(spec)))
    if not os.path.isdir(path):
        raise ValueError(f"plugin {spec}: no such directory {path}")
    return path


def _string(path: str, key: str, value: Any) -> str:
    """Returns `value` if it is a string.

    Raises:
        ValueError: If it isn't, naming the config and key.
    """
    if not isinstance(value, str):
        raise ValueError(f"{path}: {key} must be a string, not "
                         f"{type(value).__name__}")
    return value


def _check_file(path: str, what: str) -> None:
    """Raises ValueError, naming `what`, if there is no file at `path`."""
    if not os.path.isfile(path):
        raise ValueError(f"{what}: no such file {path}")
