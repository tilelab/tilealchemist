# Glossary

tilealchemist-pipeline's own terms, such as worker and shard, are in its
[glossary](https://github.com/tilelab/tilealchemist-pipeline/blob/main/docs/reference/glossary.md).

| Term | Meaning |
| --- | --- |
| archive | The existing PMTiles file a run reads, such as an OpenFreeMap or Protomaps planet. |
| archive layer | An MVT layer of the archive's tiles, such as `water`. |
| blob | One distinct tile payload in the archive, shared by every entry that points at it. |
| chunk | One task of `convert`'s process pool: consecutive entries, never splitting a blob. |
| entry | A PMTiles directory entry, mapping a run of tile IDs to a blob. |
| feature set | A named kind of data, such as surface water, that schemas provide and profiles ask for. |
| gap | A tile ID in the run's zoom range that the archive has no tile for. |
| layer | The PMTiles file a run writes for one profile. Never an MVT layer. |
| MVT | Mapbox Vector Tile, the encoding of a vector tile. |
| MVT layer | A named layer inside one tile, the `source-layer` of a map style. |
| plugin | An installed package that adds schemas or sources. |
| process | A member of `convert`'s process pool. Never called a worker. |
| profile | A Python file that turns each tile of the archive into a tile of a layer. |
| run | One build of a run config's layers. |
| run config | The TOML file listing a run's profiles, schema, zoom range, attribution and plugins. |
| schema | The code that knows how an archive encodes its data. |
| tile | A decoded tile, as a profile sees it. It stands for every tile ID that shares its blob. |
