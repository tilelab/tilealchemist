# Contributing to tilealchemist

## Set up

```sh
git clone https://github.com/tilelab/tilealchemist.git && cd tilealchemist
python3 -m venv .venv && source .venv/bin/activate
pip install -e . pylint==4.1.2 mypy==2.4.1 types-shapely
```

## Run the checks

```sh
pylint tilealchemist
mypy
python tests/smoke_test.py
```

CI's `lint.yml` runs the first two, `test.yml` the smoke test: it builds a
layer from a generated archive. To try a change on real data, see
[Test and debug a profile](docs/how-to-guides/test-and-debug-a-profile.md).

## Code

- The [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
  Docstrings are usually one "Returns …" line; `Args:` and `Raises:` only
  where they add to the types.
- Everything annotated, targets of `for`, unpacking and `with … as` on the
  line before. Annotations evaluated at runtime must work on Python 3.11.
- No checks for what the code itself guarantees. A needed check raises an
  error naming the problem and its fix; stale outside data gets a
  `log.warning()`. Only `cli/` turns errors into messages.
- GitHub-specific code lives only in `.github/`. tilealchemist never imports
  tilealchemist-pipeline.

## Documentation

`docs/` follows GitHub's
[Documentation done right](https://github.blog/developer-skills/documentation-done-right-a-developers-guide/),
sorted by [Diátaxis](https://diataxis.fr/) into tutorials, how-to guides,
reference and explanation.

- A detail of one mechanism goes in a code comment next to it, not into
  `docs/`.
- Short: one topic per document, the most important first, the
  [glossary](docs/reference/glossary.md)'s terms.
- Bold only a term where it is defined; format anything typed as code.
- Project names are lowercase, also at the start of a sentence:
  tilealchemist, tilealchemist-pipeline, tiledistillery.
- Every command and example must work as written.
- `README.md` links with full GitHub URLs: PyPI shows it without the
  repository.
- List new documents in [`docs/README.md`](docs/README.md). Long reference
  documents start with a Contents list.

## Feature sets

New feature sets are welcome if both shipped schemas can provide them, so
a profile using one runs on either. Open an issue first if the schemas
disagree on what belongs in it. A pull request adds:

- the `FeatureSet` in `tilealchemist/profiles/features.py`, its
  description saying what a schema puts in it;
- an `@feature` method in both `openmaptiles.py` and `protomaps.py`, with
  `fields` if the properties pass through;
- a row in [Shipped feature sets](docs/reference/profile-api.md#shipped-feature-sets).

Try it on real tiles of both schemas, as in
[Test and debug a profile](docs/how-to-guides/test-and-debug-a-profile.md).
Leave `archive_layers` alone: a tile without the layer gives no features,
and a stricter check would refuse archives that work today.

## Release

Set `version` in `pyproject.toml` and push the tag `v<version>`.
`release.yml` checks that they match, runs `lint.yml` and `test.yml`, and
publishes to PyPI.

Each version can be published only once, so try a release as a candidate
such as `1.0.0rc1` first and retry a failed one as `rc2`. pip takes a
candidate only when it is named, or while no final release exists:

```sh
pip install tilealchemist==1.0.0rc1
```
