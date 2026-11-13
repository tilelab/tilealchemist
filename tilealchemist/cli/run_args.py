"""The flags `convert` shares with the pipeline's commands.

Flags are kept as text and converted here. An empty value counts as unset,
so workflows can pass inputs through whether or not they were given.
"""
import argparse
from typing import TypeAlias

from tilealchemist.config import run_config
from tilealchemist.config.run_config import (
    ConvertConfig,
    with_overrides,
    zoom_level,
)

# Quoted: _SubParsersAction is not subscriptable at runtime.
Subparsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"

# (flag, help) of the config values a flag may override: only the zoom
# range, to try a config shallow before building it deep.
OVERRIDE_FLAGS: tuple[tuple[str, str], ...] = (
    ("--min-zoom", "the lowest zoom level to build (default 0)"),
    ("--max-zoom", "the highest zoom level to build (default 14)"),
)

# (flag, help) of how the transform runs.
PROCESSES_FLAG: tuple[str, str] = (
    "--transform-processes",
    "processes for the transform (default: all cores; 1 runs it inline, "
    "without a pool)")


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds the config and the zoom range flags."""
    parser.add_argument(
        "config", help="the run's .toml config, listing its profiles")
    group: argparse._ArgumentGroup = parser.add_argument_group(
        "overriding the config")
    flag: str
    text: str
    for flag, text in OVERRIDE_FLAGS:
        group.add_argument(flag, help=text)


def read_config(args: argparse.Namespace) -> ConvertConfig:
    """Returns the config with the zoom flags applied.

    Raises:
        ValueError: If the config or a flag is malformed.
        OSError: If the config cannot be read.
    """
    config: ConvertConfig = run_config.read_config(args.config)
    return with_overrides(
        config,
        min_zoom=(zoom_level(args.min_zoom, "--min-zoom")
                  if args.min_zoom else None),
        max_zoom=(zoom_level(args.max_zoom, "--max-zoom")
                  if args.max_zoom else None))


def read_processes(args: argparse.Namespace) -> int | None:
    """Returns --transform-processes, or None for one per core.

    Raises:
        ValueError: If it is malformed or below one.
    """
    return (count(args.transform_processes, PROCESSES_FLAG[0])
            if args.transform_processes else None)


def count(text: str, flag: str) -> int:
    """Returns `text` as a count of at least one.

    Raises:
        ValueError: If it isn't one, naming `flag`.
    """
    try:
        value: int = int(text)
    except ValueError:
        raise ValueError(f"{flag} {text!r} is not a whole number") from None
    if value < 1:
        raise ValueError(f"{flag} must be at least 1, not {value}")
    return value
