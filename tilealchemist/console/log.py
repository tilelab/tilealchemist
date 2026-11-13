"""Warnings and folded log groups, formatted by orchestrator templates.

Each template is read from an environment variable and may use `{title}` and
`{message}`; unset, the log is plain. See docs/reference/cli.md.
"""
import contextlib
import os
import re
import sys
from collections.abc import Iterator

WARNING_VARIABLE: str = "TILEALCHEMIST_LOG_WARNING"
GROUP_VARIABLE: str = "TILEALCHEMIST_LOG_GROUP"
END_GROUP_VARIABLE: str = "TILEALCHEMIST_LOG_END_GROUP"

_DEFAULTS: dict[str, str] = {
    WARNING_VARIABLE: "warning: {title}: {message}",
    GROUP_VARIABLE: "{title}:",
    END_GROUP_VARIABLE: "",
}

_PLACEHOLDER: re.Pattern[str] = re.compile(r"\{(\w+)\}")


def _write(variable: str, **values: str) -> None:
    """Writes one templated line to stderr, unless it comes out empty.

    Placeholders are filled in one pass, so values are never re-expanded and
    unknown placeholders stay as written.

    Args:
        variable: The environment variable holding the template.
        **values: The text for each placeholder.
    """
    template: str = os.environ.get(variable) or _DEFAULTS[variable]
    line: str = _PLACEHOLDER.sub(lambda match: values.get(match[1], match[0]),
                                 template)
    if line:
        print(line, file=sys.stderr)


def warning(title: str, message: str) -> None:
    """Writes a one-line warning to stderr."""
    _write(WARNING_VARIABLE, title=title, message=message)


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    """Folds whatever the block writes to stderr under `title`."""
    _write(GROUP_VARIABLE, title=title)
    try:
        yield
    finally:
        _write(END_GROUP_VARIABLE, title=title)
