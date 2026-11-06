"""`tilealchemist convert`; the conversion is in conversion/convert.py."""
import argparse
import os
import sys

from tilealchemist.cli import run_args
from tilealchemist.config import run_config
from tilealchemist.config.run_config import ConvertConfig
from tilealchemist.conversion.convert import convert

HELP: str = """Builds every layer of a run from a PMTiles file on this machine.

    tilealchemist convert planet.pmtiles land.toml
    tilealchemist convert planet.pmtiles land.toml --max-zoom 6 -o ~/layers

Writes <layer>.pmtiles into the output directory. Converts the tiles the
archive holds, and nothing for tile ids it lacks. Run `tilealchemist
setup` first if the run has plugins or profile dependencies.
"""


def add_parser(subparsers: run_args.Subparsers) -> None:
    """Adds `convert` to the top-level subparsers."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "convert", help="build a run's layers from a local PMTiles file",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="the PMTiles file to read")
    run_args.add_config_arguments(parser)
    group: argparse._ArgumentGroup = parser.add_argument_group("this machine")
    group.add_argument(run_args.PROCESSES_FLAG[0],
                       help=run_args.PROCESSES_FLAG[1])
    group.add_argument(
        "-o", "--output-dir", default=".",
        help="where the layers go (default: the current directory)")
    group.add_argument(
        "--overwrite", action="store_true",
        help="replace a layer file already in the output directory")
    parser.set_defaults(run=run, parser=parser)


def run(args: argparse.Namespace) -> None:
    """Converts the archive."""
    parser: argparse.ArgumentParser = args.parser
    config: ConvertConfig
    processes: int | None
    try:
        config = run_args.read_config(args)
        run_config.check(config)
        processes = run_args.read_processes(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not os.path.isfile(args.input):
        parser.error(f"no such file: {args.input}")
    try:
        convert(config=config, input_path=args.input,
                output_dir=args.output_dir, processes=processes,
                overwrite=args.overwrite)
    except (OSError, ValueError) as error:
        # A profile's own error arrives as a RuntimeError, with traceback.
        sys.exit(f"tilealchemist convert: {error}")
