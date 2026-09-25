"""Enforces this repository's documentation style; see docs/DOC_STYLE.md."""
import argparse
import ast
import io
import pathlib
import re
import sys
import textwrap
import tokenize

DIRECTIVE = re.compile(
    r"^#\s*(?:!|-\*-|coding[:=]|noqa\b|type:\s|pragma:|fmt:\s*(?:on|off|skip)\b"
    r"|(?:ruff|flake8|mypy|pylint|pyright|isort|black|nosec|codespell):)"
)
SECTIONS = ("Args", "Attributes", "Examples", "Note", "Raises", "Returns",
            "Yields")
SECTION_HEADER = re.compile(r"^([A-Z][A-Za-z]*):$")
ARGUMENT_ENTRY = re.compile(r"^(\*{0,2}[A-Za-z_]\w*)\s*(?:\([^)]*\))?:")
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_directive(text):
    """Report whether a comment is an instruction to another tool.

    Args:
        text: The comment's source text, including its leading ``#``.

    Returns:
        True for shebangs, coding declarations and tool pragmas, which every
        rule here exempts.
    """
    return bool(DIRECTIVE.match(text.strip()))


def _describe(node):
    """Name a definition the way a diagnostic should refer to it.

    Args:
        node: A module, class or function node.

    Returns:
        A short phrase such as ``function 'run_prepare'``.
    """
    if isinstance(node, ast.Module):
        return "module"
    kind = "class" if isinstance(node, ast.ClassDef) else "function"
    return f"{kind} {node.name!r}"


def _documentable(tree):
    """Walk the definitions that this style expects a docstring on.

    Args:
        tree: The parsed module.

    Yields:
        ``(node, is_method)`` for the module and for every class and
        top-level-or-method function. Definitions nested inside a function are
        local helpers, and are skipped along with anything inside them.
    """
    stack = [(tree, False, False)]
    while stack:
        node, is_method, inside_function = stack.pop()
        if not inside_function:
            yield node, is_method
        in_class = isinstance(node, ast.ClassDef)
        in_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                stack.append(
                    (child, in_class, inside_function or in_function))


def _parameters(node, is_method):
    """Collect the parameter names a docstring's ``Args:`` section may name.

    Args:
        node: The function whose signature to read.
        is_method: True when the function is defined directly in a class body,
            whose first parameter is the instance or class and is never
            documented.

    Returns:
        The parameter names with any ``*`` or ``**`` stripped, so that
        ``*args`` and ``args`` are accepted for the same parameter.
    """
    arguments = node.args
    names = [parameter.arg for parameter
             in arguments.posonlyargs + arguments.args]
    if is_method and names and names[0] in ("self", "cls"):
        names = names[1:]
    names += [parameter.arg for parameter in arguments.kwonlyargs]
    names += [parameter.arg for parameter
              in (arguments.vararg, arguments.kwarg) if parameter]
    return names


def _sections(body):
    """Split a docstring's body into its Google-style sections.

    Args:
        body: The docstring below its summary line, dedented.

    Returns:
        ``(found, unknown)``, where ``found`` maps a recognised section name to
        its lines and ``unknown`` lists ``(name, offset)`` for headers that are
        not part of this style.
    """
    found, unknown, current = {}, [], None
    for offset, line in enumerate(body):
        header = SECTION_HEADER.match(line)
        if not header:
            if current:
                found[current].append(line)
            continue
        name = header.group(1)
        if name in SECTIONS:
            current = name
            found.setdefault(name, [])
        else:
            current = None
            unknown.append((name, offset))
    return found, unknown


def _check_arguments(node, is_method, entries, line, problems):
    """Compare a docstring's ``Args:`` section against the real signature.

    Args:
        node: The documented function.
        is_method: Whether to drop a leading ``self``/``cls``.
        entries: The lines of the ``Args:`` section.
        line: The line the docstring starts on, used to anchor a diagnostic.
        problems: The list each finding is appended to.
    """
    documented = [match.group(1).lstrip("*") for match
                  in (ARGUMENT_ENTRY.match(entry.strip()) for entry in entries)
                  if match]
    if not documented:
        return
    actual = _parameters(node, is_method)
    for name in documented:
        if name not in actual:
            problems.append((line, "TA506",
                             f"{_describe(node)} documents a parameter {name!r} "
                             "that its signature does not take"))
    for name in actual:
        if name not in documented:
            problems.append((line, "TA506",
                             f"{_describe(node)} takes {name!r} but its Args "
                             "section does not document it"))


def _check_docstring(node, is_method, text, line, problems):
    """Check one docstring's shape against the Google style.

    Args:
        node: The module, class or function the docstring belongs to.
        is_method: Whether the node is a method, for signature comparison.
        text: The docstring's value.
        line: The line the docstring starts on.
        problems: The list each finding is appended to.
    """
    lines = text.splitlines()
    summary = lines[0].strip() if lines else ""
    if not summary:
        problems.append((line, "TA504",
                         f"{_describe(node)} opens its docstring with a blank "
                         "line; the summary goes on the first line"))
        return
    if not summary.endswith((".", "?", "!", ":")):
        problems.append((line, "TA504",
                         f"docstring summary for {_describe(node)} does not "
                         "end in a full stop"))
    if len(lines) > 1 and lines[1].strip():
        problems.append((line, "TA504",
                         f"docstring for {_describe(node)} needs a blank "
                         "line between its summary and the rest"))
    body = textwrap.dedent("\n".join(lines[1:])).splitlines()
    found, unknown = _sections(body)
    for name, offset in unknown:
        problems.append((line + 1 + offset, "TA505",
                         f"{name!r} is not a section of this style; use one of "
                         + ", ".join(SECTIONS)))
    for name, entries in found.items():
        if not any(entry.strip() for entry in entries):
            problems.append((line, "TA505",
                             f"{_describe(node)} has an empty {name!r} section"))
    if "Args" in found and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _check_arguments(node, is_method, found["Args"], line, problems)


def _check_documentation(tree, problems, require):
    """Check every documentable definition in a module.

    Args:
        tree: The parsed module.
        problems: The list each finding is appended to.
        require: Whether a missing docstring is itself a finding.
    """
    for node, is_method in _documentable(tree):
        text = ast.get_docstring(node, clean=False)
        if text is None or not text.strip():
            if require:
                problems.append((getattr(node, "lineno", 1),
                                 "TA503", f"{_describe(node)} has no docstring"))
            continue
        line = node.body[0].value.lineno
        _check_docstring(node, is_method, text, line, problems)


def _check_blocks(own_line, lines, problems):
    """Check that each own-line comment is single and attached to its code.

    Args:
        own_line: ``(line number, text)`` for every comment on a line of its
            own, in source order.
        lines: The file's source lines.
        problems: The list each finding is appended to.
    """
    numbers = {number for number, _ in own_line}
    for number, _text in own_line:
        if number - 1 in numbers:
            continue
        run = 1
        while number + run in numbers:
            run += 1
        if run > 1:
            problems.append((number, "TA501",
                             f"comment block spans {run} lines; a comment is a "
                             "single line, and a longer explanation belongs in "
                             "a docstring or docs/"))
        following = lines[number + run - 1:]
        if not following or not following[0].strip():
            problems.append((number, "TA502",
                             "comment is not attached to the code it comments "
                             "on; put it directly above that line"))


def check(path, require_docstrings=True):
    """Check one file against this repository's documentation style.

    Args:
        path: The file to read.
        require_docstrings: Whether an undocumented definition is a finding.

    Returns:
        ``(line, code, message)`` findings, sorted by line.
    """
    source = path.read_text()
    problems = []
    try:
        tree = ast.parse(source, filename=str(path))
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError) as error:
        return [(getattr(error, "lineno", 1) or 1, "TA500", f"cannot parse: {error}")]
    lines = source.splitlines()
    comments = [t for t in tokens
                if t.type == tokenize.COMMENT and not _is_directive(t.string)]
    own_line = [(t.start[0], t.string) for t in comments
                if not lines[t.start[0] - 1][:t.start[1]].strip()]
    _check_blocks(own_line, lines, problems)
    _check_documentation(tree, problems, require_docstrings)
    return sorted(problems)


def main(argv=None):
    """Check the given paths and report every finding.

    Args:
        argv: Command line arguments, or None to read ``sys.argv``.

    Returns:
        1 if anything was reported, 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=pathlib.Path,
                        default=[pathlib.Path(".")])
    parser.add_argument("--allow-missing", action="store_true",
                        help="do not report definitions that have no docstring")
    arguments = parser.parse_args(argv)
    targets = sorted({file for path in arguments.paths
                      for file in ([path] if path.is_file() else path.rglob("*.py"))
                      if ".claude" not in file.parts and "build" not in file.parts})
    problems = [(file, *problem) for file in targets
                for problem in check(file, not arguments.allow_missing)]
    for file, line, code, message in problems:
        print(f"{file}:{line}: {code} {message}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
