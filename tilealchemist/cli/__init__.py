"""The `tilealchemist` command; each subcommand parses, then hands off.

Other packages add commands as entry points in COMMAND_GROUP, like
tilealchemist-pipeline's `distributed`.
"""
import argparse
import importlib.metadata
from collections.abc import Callable, Sequence

from tilealchemist.cli import convert, run_args, setup
from tilealchemist.config import plugins

COMMAND_GROUP: str = "tilealchemist.commands"

HELP: str = """Builds PMTiles layers from an existing PMTiles archive, one
profile per layer deciding what each tile becomes.

    tilealchemist convert planet.pmtiles land.toml

See https://github.com/tilelab/tilealchemist for writing a profile.
"""


def parser() -> argparse.ArgumentParser:
    """Returns the command's parser with every subcommand."""
    top: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="tilealchemist", description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument(
        "--version", action="version",
        version=f"%(prog)s {importlib.metadata.version('tilealchemist')}")
    commands: run_args.Subparsers = top.add_subparsers(
        title="commands", metavar="COMMAND", required=True)
    convert.add_parser(commands)
    setup.add_parser(commands)
    name: str
    for name in plugins.names(COMMAND_GROUP):
        add_parser: Callable[[run_args.Subparsers], None] = plugins.load(
            COMMAND_GROUP, "command", name)
        add_parser(commands)
    return top


def main(argv: Sequence[str] | None = None) -> None:
    """Runs the command with `argv`, or the command line's arguments."""
    args: argparse.Namespace = parser().parse_args(argv)
    args.run(args)
