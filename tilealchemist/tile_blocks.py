"""Fixed tile-id blocks, the unit a profile's measured seconds are kept under; see docs/ARCHITECTURE.md "Measured tile blocks"."""
import bisect

# 4**6 = 4096 tiles: one z8 cell at z14, and a whole zoom level up to z6.
TILE_BLOCK_BITS = 12

# Where each zoom level's tile ids start: PMTiles numbers z0 first, then z1, and so on.
ZOOM_BASES = [(4 ** zoom - 1) // 3 for zoom in range(32)]


def tile_block(tile_id):
    """The block a tile id belongs to.

    A block is a run of `2 ** TILE_BLOCK_BITS` consecutive Hilbert indices
    within one zoom level -- a quadtree cell -- and never spans two zoom
    levels. It depends on nothing but the tile id, so the same block means the
    same piece of the world in every run, whichever archive build, zoom range
    or worker count that run has.

    Args:
        tile_id: A PMTiles tile id.

    Returns:
        The first tile id of its block, which is the block's key.
    """
    zoom_base = ZOOM_BASES[bisect.bisect_right(ZOOM_BASES, tile_id) - 1]
    return zoom_base + ((tile_id - zoom_base) >> TILE_BLOCK_BITS << TILE_BLOCK_BITS)


def profile_combo_key(names):
    """What a set of profiles' measured block seconds are filed under.

    The whole set rather than each profile alone: profiles share derived work
    through `Tile.derived()`, and whichever of them runs first is billed for
    it, so one profile's seconds only mean anything beside the same others.

    Args:
        names: The profiles' names, in any order.

    Returns:
        The names sorted and joined with `+`, safe in a path segment.
    """
    return "+".join(sorted(names))


def format_block_seconds(block_seconds):
    """Render per-block seconds as one usage-line field value.

    Args:
        block_seconds: A mapping of block key to seconds.

    Returns:
        `block:seconds` per block in key order, separated by `|`, or `-` where
        there are none.
    """
    return "|".join(f"{block}:{seconds:.6g}"
                    for block, seconds in sorted(block_seconds.items())) or "-"


def parse_block_seconds(value):
    """Read per-block seconds back from a usage-line field value.

    Args:
        value: The field as `format_block_seconds()` wrote it.

    Returns:
        A mapping of block key to seconds.
    """
    if not value or value == "-":
        return {}
    pairs = (item.split(":") for item in value.split("|"))
    return {int(block): float(seconds) for block, seconds in pairs}
