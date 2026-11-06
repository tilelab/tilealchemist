# Run config reference

The run config is a TOML file that says what a run builds. `setup`,
`convert` and tilealchemist-pipeline's `distributed plan` each take one.

```toml
# layers.toml
profiles = ["land.py", "../elsewhere/waterways.py"]
schema = "protomaps"
min_zoom = 0
max_zoom = 14
attribution = '<a href="https://example.org">&copy; Example</a> {source}'
plugins = ["./my_plugin", "tilealchemist-borders"]
```

The file name must end in `.toml`. Relative paths are relative to the file.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `profiles` | Array of paths | Required | One layer per profile, named after its `name`. Names must be unique. |
| `schema` | String | `"openmaptiles"` | The archive's schema: `"openmaptiles"` (such as OpenFreeMap), `"protomaps"`, or one a plugin installs. |
| `min_zoom`, `max_zoom` | Integer, 0 to 30 | `0`, `14` | The zoom range. `--min-zoom` and `--max-zoom` override it, and `convert` narrows it to the archive's. |
| `attribution` | String | Unset | A template for what each layer credits; see below. |
| `plugins` | Array of strings | `[]` | Pip requirements `setup` installs: a PyPI name, a URL requirement, or a directory starting with `.`, `/` or `~`. |

Any other key is an error, as is a wrong type, a missing file, or a
`min_zoom` above `max_zoom`.

## attribution

`{source}` stands for the attribution the archive declares in its metadata:

| Value | The layer credits |
| --- | --- |
| Unset or blank | The archive's attribution. |
| `'<a href="https://example.org">&copy; Example</a> {source}'` | You, then the archive. |
| `'&copy; Example'` | Exactly this, so it must include what the data's license requires. |

It fails before the first tile when the archive declares no attribution and
the template is unset or contains `{source}`. Then set the full credit
without `{source}`.
