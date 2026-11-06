"""The zoom levels a run can cover, and their tile ids."""
from enum import IntEnum

from pmtiles.tile import zxy_to_tileid


# A class rather than the functional API, so type checkers accept it as a
# type and pickle can carry it into a pool process.
class ZoomLevel(IntEnum):
    """A zoom level a run can be walked at."""

    Z0 = 0
    Z1 = 1
    Z2 = 2
    Z3 = 3
    Z4 = 4
    Z5 = 5
    Z6 = 6
    Z7 = 7
    Z8 = 8
    Z9 = 9
    Z10 = 10
    Z11 = 11
    Z12 = 12
    Z13 = 13
    Z14 = 14
    Z15 = 15
    Z16 = 16
    Z17 = 17
    Z18 = 18
    Z19 = 19
    Z20 = 20
    Z21 = 21
    Z22 = 22
    Z23 = 23
    Z24 = 24
    Z25 = 25
    Z26 = 26
    Z27 = 27
    Z28 = 28
    Z29 = 29
    # tile_id_bounds() needs max_zoom + 1, and tile ids overflow int64 past 31.
    Z30 = 30


MAX_SUPPORTED_ZOOM: int = max(ZoomLevel).value


def tile_id_bounds(min_zoom: ZoomLevel, max_zoom: ZoomLevel) -> tuple[int, int]:
    """Returns the half-open tile id range `[start, limit)` of a zoom range."""
    start: int = zxy_to_tileid(min_zoom, 0, 0)
    limit: int = zxy_to_tileid(max_zoom + 1, 0, 0)
    return start, limit
