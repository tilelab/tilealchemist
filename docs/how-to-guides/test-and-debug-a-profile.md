# Test and debug a profile

A planet takes hours. Try a profile on a small extract, in one process:

```sh
pmtiles extract planet.pmtiles berlin.pmtiles --bbox=13.08,52.33,13.76,52.68 --maxzoom=12
tilealchemist convert berlin.pmtiles layers.toml -o test --overwrite --transform-processes 1
pmtiles show test/land.pmtiles --metadata
```

- `pmtiles extract` from [go-pmtiles](https://github.com/protomaps/go-pmtiles/releases)
  also reads a URL and downloads only what it extracts. Tile data roughly
  triples with each zoom level, so keep the zoom low or the box small.
- `-o test --overwrite` keeps tries apart from real layers and replaces them
  each time.
- `--transform-processes 1` runs the profiles in the main process, so
  `breakpoint()` and debuggers work.
- To see the tiles, drop the file onto [pmtiles.io](https://pmtiles.io).

## Common errors

- `RuntimeError: profile 'land' failed on tile 9/269/151`: the profile
  raised. The traceback above it points at the line.
- `land.py needs module 'scipy', which is not installed`: declare the
  package in the profile's PEP 723 block and run `tilealchemist setup`; see
  [the profile file](../reference/profile-api.md#the-profile-file).
- `no tiles: test/waterways.pmtiles left out`: the profile returned nothing
  for every tile. Check the zoom range: Protomaps has no waterway lines
  below zoom level 9.
- `berlin.pmtiles has no waterway layer, so it isn't in schema openmaptiles`:
  set the run config's `schema` to the one the message names.
