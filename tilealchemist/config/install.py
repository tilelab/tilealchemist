"""Installing a run's plugins and its profiles' PEP 723 dependencies.

They go into tilealchemist's own environment, by pip, or by uv where there
is no pip (as in a `uv tool install`), constrained to the installed
tilealchemist so nothing can swap it out. See docs/reference/cli.md.
"""
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from typing import TextIO

from tilealchemist.profiles.requirements import profile_requirements

PACKAGE: str = "tilealchemist"


def setup(plugins: Iterable[str], profile_paths: Iterable[str]) -> None:
    """Installs the plugins, then the profiles' dependencies.

    Plugins go first, so pip reconciles a profile asking for one of them in
    another version.

    Args:
        plugins: Pip requirements; directories are absolute paths.
        profile_paths: The profiles' files.

    Raises:
        ValueError: If a profile's metadata block is malformed.
        OSError: If a profile cannot be read.
        RuntimeError: If there is no pip or uv, or the install fails.
    """
    requirements: list[str] = []
    path: str
    for path in profile_paths:
        try:
            requirements += profile_requirements(path)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from None
    _install("plugins", list(dict.fromkeys(plugins)))
    _install("the profiles' dependencies", list(dict.fromkeys(requirements)))


def _install(what: str, requirements: list[str]) -> None:
    """Installs requirements into this interpreter's environment.

    Args:
        what: What they are, for the log and errors.
        requirements: PEP 508 requirements; may be empty.

    Raises:
        RuntimeError: If there is no pip or uv, or the install fails.
    """
    if not requirements:
        print(f"{what}: none to install", file=sys.stderr)
        return
    installer: list[str] | None = _installer()
    if installer is None:
        raise RuntimeError(f"cannot install {what} ({' '.join(requirements)}): "
                           f"this environment has no pip, and there is no uv "
                           f"on PATH; install them into {sys.prefix} yourself")
    version: str = importlib.metadata.version(PACKAGE)
    print(f"installing {what}: {' '.join(requirements)}", file=sys.stderr)
    directory: str
    handle: TextIO
    with tempfile.TemporaryDirectory() as directory:
        constraints: str = os.path.join(directory, "constraints.txt")
        with open(constraints, "w", encoding="utf-8") as handle:
            handle.write(f"{PACKAGE}=={version}\n")
        command: list[str] = [*installer, "--constraint", constraints,
                              *requirements]
        if subprocess.run(command, check=False).returncode:
            raise RuntimeError(f"installing {what} failed; if pip reports a "
                               f"conflict over {PACKAGE}, one of them needs "
                               f"another {PACKAGE} than this one, {version}")


def _installer() -> list[str] | None:
    """Returns the install command for this environment, or None."""
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip", "install"]
    uv: str | None = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    return None
