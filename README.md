# TileAlchemist

TileAlchemist builds new map layers from an existing OpenStreetMap vector
tile archive. You write a **profile**, a short Python file that decides what
each tile of your layer contains, and `tilealchemist convert` runs it over
every tile of a PMTiles archive such as an OpenFreeMap or Protomaps planet.
Each profile becomes a PMTiles file of its own, for example a land layer:
each tile minus its water.

It reads archives on the local disk in the OpenMapTiles or Protomaps schema,
decodes each distinct tile once for all profiles, and uses every CPU core.
To build from a remote archive across many machines, use
[tilealchemist-pipeline](https://github.com/tilelab/tilealchemist-pipeline);
to build from raw OpenStreetMap extracts, use
[TileDistillery](https://github.com/foxandfeature/tiledistillery).

## Getting started

```sh
pip install tilealchemist
```

Requires Python 3.11 or later. Then follow
[Build your first layer](docs/tutorials/build-your-first-layer.md), about 15
minutes. The [documentation](docs/README.md) covers the rest. To work on
tilealchemist itself, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License and attribution

The code is [MIT](LICENSE)-licensed. The data in every layer derives from
OpenStreetMap (© OpenStreetMap contributors, ODbL) and must be attributed.
Each layer carries the attribution of the archive it was built from; see
[`attribution`](docs/reference/run-config.md#attribution).
