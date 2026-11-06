"""Loads a profile from its .py file."""
import importlib.util
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

from tilealchemist.profiles.base import Profile


def load_profile(path: str | Path) -> type[Profile]:
    """Returns the `PROFILE` class of the profile file at `path`.

    Raises:
        ValueError: If the file doesn't exist, imports a module that isn't
            installed, or has no `PROFILE`.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"profile file not found: {path}")
    spec: ModuleSpec | None = importlib.util.spec_from_file_location(
        path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path} as a Python module")
    module: ModuleType = importlib.util.module_from_spec(spec)
    # Not put in sys.modules: pool processes load it again by path.
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as error:
        raise ValueError(
            f"{path} needs module {error.name!r}, which is not installed; "
            f"declare it in the profile's PEP 723 block and run "
            f"`tilealchemist setup`") from error
    if not hasattr(module, "PROFILE"):
        raise ValueError(f"{path} has no module-level "
                         f"`PROFILE = YourProfileClass` assignment")
    profile_class: type[Profile] = module.PROFILE
    return profile_class
