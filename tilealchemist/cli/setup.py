"""`tilealchemist setup`; the install is in config/install.py."""
import argparse
import sys

from tilealchemist.cli import run_args
from tilealchemist.config import install
from tilealchemist.config.run_config import ConvertConfig, read_config

HELP: str = """Installs the run's plugins, then each profile's dependencies,
declared in a PEP 723 block in the profile:

    # /// script
    # dependencies = ["scipy"]
    # ///

Run it before `convert`. Installs with pip, or uv if the environment has
no pip.

    tilealchemist setup land.toml
"""


def add_parser(subparsers: run_args.Subparsers) -> None:
    """Adds `setup` to the top-level subparsers."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "setup", help="install the run's plugins and profile dependencies",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="the run's .toml config")
    parser.set_defaults(run=run, parser=parser)


def run(args: argparse.Namespace) -> None:
    """Installs the plugins and profile dependencies."""
    parser: argparse.ArgumentParser = args.parser
    config: ConvertConfig
    try:
        config = read_config(args.config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        install.setup(config.plugins, config.profiles)
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"tilealchemist setup: {error}")
