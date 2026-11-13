"""The `tilealchemist` command; each subcommand parses, then hands off."""
import argparse
import importlib.metadata
from collections.abc import Sequence

from tilealchemist.cli import convert, run_args, setup

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
    return top


def main(argv: Sequence[str] | None = None) -> None:
    """Runs the command with `argv`, or the command line's arguments."""
    args: argparse.Namespace = parser().parse_args(argv)
    args.run(args)
