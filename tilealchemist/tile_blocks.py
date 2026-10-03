"""Fixed tile-id blocks, the unit measured profile costs are kept under.

See docs/ARCHITECTURE.md "Measured tile blocks".
"""
import bisect

# 4**6 = 4096 tiles: one z8 cell at z14, and a whole zoom level up to z6.
TILE_BLOCK_BITS = 12

# Where each zoom level's tile ids start: PMTiles numbers z0 first, then z1, and
# so on.
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
    offset = (tile_id - zoom_base) >> TILE_BLOCK_BITS << TILE_BLOCK_BITS
    return zoom_base + offset


def home_blocks(records):
    """The block each record is measured, priced and partitioned under.

    That is the block of the record that decodes its blob -- the first of its
    `(offset, length)` run in offset order, which is the lowest tile id
    pointing there -- and not the record's own. A deduplicated tile points
    back at the blob its first copy wrote, so keyed by its own block it would
    drag that far-away offset into whichever worker holds its tile, and every
    such worker paid a range request per stretch of them: 84 on worker 2 of
    standardprofiles run 37037515494, against 1 on worker 0, which nothing
    pointed back before. Keyed by its home, a run of consecutive blocks is
    one stretch of the archive's bytes.

    Args:
        records: Real manifest records, in offset order.

    Yields:
        The home block of each record, in the same order.
    """
    previous_key = home = None
    for record in records:
        key = (record.offset, record.length)
        if key != previous_key:
            home, previous_key = tile_block(record.tile_id), key
        yield home


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


def format_block_values(block_values):
    """Render one figure per block -- seconds, or bytes -- as one field value.

    Args:
        block_values: A mapping of block key to its figure.

    Returns:
        `block:value` per block in key order, separated by `|`, or `-` where
        there are none. A byte count stays whole; seconds keep six digits.
    """
    return "|".join(f"{block}:{_format_block_value(value)}"
                    for block, value in sorted(block_values.items())) or "-"


def _format_block_value(value):
    """Format one block's figure: a byte count whole, seconds to six digits.

    Args:
        value: The figure, an int for bytes or a float for seconds.

    Returns:
        The figure as text.
    """
    return str(value) if isinstance(value, int) else format(value, ".6g")


def parse_block_values(value):
    """Read one figure per block back from a usage-line field value.

    Args:
        value: The field as `format_block_values()` wrote it.

    Returns:
        A mapping of block key to its figure.
    """
    if not value or value == "-":
        return {}
    pairs = (item.split(":") for item in value.split("|"))
    return {int(block): float(figure) for block, figure in pairs}
