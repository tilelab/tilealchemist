# Documentation style

Documentation here lives at three altitudes, and each has one job.

**Docstrings** describe the interface: what a module, class or function is
for, what it takes, what it gives back, and how it fails. Every module,
class, and top-level or method function carries one. Write as much as the
thing needs — a one-line summary is enough for a small helper, and a full
`Args`/`Returns`/`Raises` breakdown is right for anything a caller has to get
right.

**Comments** are for a single local fact the code cannot state about itself:
a measured constant, a precondition a future edit would silently break, an
upstream bug being worked around. One line, sitting directly on the line it
is about.

**`docs/`** holds everything wider than one definition: how the pieces fit,
why a design was chosen, what a production run cost. That is
[`ARCHITECTURE.md`](ARCHITECTURE.md), [`PROFILES.md`](PROFILES.md), and the
[README](../README.md).

`lint/doc_style.py` enforces this. It runs on every push and pull request
(`.github/workflows/lint.yml`) and takes paths to check:

```sh
python3 lint/doc_style.py tilealchemist lint
```

## The rules

| Code | Rule |
| --- | --- |
| `TA500` | The file could not be parsed. |
| `TA501` | A comment is one line. No stacked `#` lines forming a paragraph. |
| `TA502` | A comment is attached to the code it comments on: the next line is that code, with no blank line between them. |
| `TA503` | Every module, class, and top-level or method function has a docstring. |
| `TA504` | A docstring opens with a summary line that ends in a full stop, and anything further is separated from it by a blank line. |
| `TA505` | A section heading is one of `Args`, `Attributes`, `Examples`, `Note`, `Raises`, `Returns`, `Yields`, and is not empty. |
| `TA506` | An `Args:` section names exactly the parameters the signature takes. |

Functions defined inside another function are local helpers; `TA503` skips
them, and so does everything below them. Tool directives — `# noqa`,
`# type:`, `# fmt: off`, `# ruff:`, shebangs, coding declarations — are exempt
from the comment rules; they are instructions to other programs, not prose.

`--allow-missing` turns `TA503` off, for checking the format of what is
already written without being told about what is not.

## The docstring shape

```python
def fetch_range(url, start, end, *, retries=3):
    """Fetch a byte range from url.

    Args:
        url: Absolute URL of the source archive.
        start: First byte offset, inclusive.
        end: Last byte offset, inclusive.
        retries: Attempts before giving up.

    Returns:
        The raw bytes of the requested range.

    Raises:
        RangeUnsupported: If the server ignores the Range header.
    """
```

The summary is one line in the imperative, saying what the thing does. Sections
follow after a blank line, each heading at the docstring's own indentation and
its entries indented one level further.

Keep a docstring to its subject. A docstring says what this function is for
and how to call it; it is not the place for why the surrounding design looks
the way it does, which is `docs/`. A pointer across is fine and often better
than a paragraph:

```python
    """Return the shard's offsets in the order the archive stores them.

    Offset order, not tile order; see docs/ARCHITECTURE.md "Fetching" for why
    a shard is walked this way.
    """
```

`TA506` exists because an `Args:` section is the part that rots. A renamed
parameter with a stale docstring is worse than no docstring, so the check
compares the two on every run. It only fires when an `Args:` section is
present — a function whose parameters are obvious from the summary does not
need one.

## Comments discuss the code in front of them

A comment describes the code directly below it, and only that code. This is
why `TA502` rejects a blank line after a comment: the blank line is what turns
a comment about the next statement into a floating header about a region.

```python
# Wrong: a header over a region, detached from any one statement.
#
# It also explains a decision, which belongs in a docstring or docs/.

entries = sorted(...)
```

```python
# Offset order, not tile order; see docs/ARCHITECTURE.md "Fetching".
entries = sorted(raw, key=operator.attrgetter("offset"))
```

There is no cap on how many a file may carry, but a comment is still the
narrowest of the three altitudes. Reaching for a second and third in one
function usually means something else wants doing:

1. The explanation is really about the interface. It belongs in the docstring.
2. The rationale is really documentation. Move it to `ARCHITECTURE.md` and
   leave a one-line pointer, or none if the code reads fine without one.
3. The names are carrying less than they could. A comment explaining what a
   variable holds is a variable that wants renaming.
4. The function is doing several jobs, and wants splitting.
