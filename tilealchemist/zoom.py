"""The closed set of zoom levels this pipeline can walk."""
from enum import IntEnum

# tile_id_bounds() asks zxy_to_tileid() for max_zoom + 1, which overflows int64
# above 31.
MAX_SUPPORTED_ZOOM = 30

# `module`/`qualname` make functional-API members picklable, which ChunkJob
# needs.
ZoomLevel = IntEnum(
    "ZoomLevel",
    {f"Z{level}": level for level in range(MAX_SUPPORTED_ZOOM + 1)},
    module=__name__,
    qualname="ZoomLevel",
)
