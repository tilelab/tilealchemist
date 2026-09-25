"""A URL in, every directory entry covering the zoom range out, in offset order."""
import sys

from pmtiles.tile import deserialize_directory, deserialize_header, zxy_to_tileid

from tilealchemist.ranged_fetch import DownloadProgress, fetch_range
from tilealchemist.throttle import UpdateLineThrottle

PMTILES_HEADER_LENGTH = 127

# PMTiles v3 section 4 requires header plus root inside the first 16,384 bytes.
HEADER_AND_ROOT_PREFIX_LENGTH = 16 * 1024

LOG_INTERVAL = 1.0
RETRY_LABEL = "prepare-shards"


class WalkProgress:
    """Prints how far the directory walk has got, at most once per interval.

    Attributes:
        total_bytes: The size of the leaf window being decoded.
        entries: The list the walk appends to, read for its current length.
        directories_decoded: Directories deserialized so far, root included.
        directories_popped: Directories walked to the end so far.
        decoded_bytes: Bytes of leaf directory deserialized so far.
        throttle: The rate limiter the lines go through.
    """

    def __init__(self, total_bytes, entries):
        """Set up progress reporting for one directory walk.

        Args:
            total_bytes: The size of the leaf window being decoded.
            entries: The list the walk appends its entries to.
        """
        self.total_bytes = total_bytes
        self.entries = entries
        self.directories_decoded = 1
        self.directories_popped = 0
        self.decoded_bytes = 0
        self.throttle = UpdateLineThrottle(LOG_INTERVAL)

    def decoded(self, node_bytes):
        """Record one more directory deserialized.

        Args:
            node_bytes: That directory's raw bytes, counted towards the total.
        """
        self.directories_decoded += 1
        self.decoded_bytes += len(node_bytes)
        self.report()

    def popped(self):
        """Record one more directory walked to the end."""
        self.directories_popped += 1
        self.report()

    def report(self):
        """Print how far the walk has got, if a line is due."""
        if not self.throttle.due():
            return
        percent = (f" (~{100 * self.decoded_bytes / self.total_bytes:.1f}%)"
                    if self.total_bytes else "")
        print(f"update: decoded {self.directories_decoded} directories, "
              f"{self.directories_popped} processed, "
              f"{len(self.entries)} entries so far{percent}", file=sys.stderr)


class LeafWindow:
    """The one slice of the leaf section a walk needs, held in memory.

    Attributes:
        blob: The fetched bytes.
        start: The offset within the leaf section that `blob` begins at.
    """

    def __init__(self, blob, start):
        """Hold a fetched slice of the leaf section.

        Args:
            blob: The fetched bytes.
            start: The offset within the leaf section that `blob` begins at.
        """
        self.blob = blob
        self.start = start

    def node_bytes(self, entry):
        """Cut one directory out of the window.

        Args:
            entry: The root entry pointing at that directory.

        Returns:
            The directory's bytes.

        Raises:
            RuntimeError: If the directory lies outside the window, which means
                the archive orders its leaves in a way this reader cannot follow
                without fetching the whole leaf section.
        """
        offset = entry.offset - self.start
        if offset < 0 or offset + entry.length > len(self.blob):
            raise RuntimeError(
                f"directory at leaf-section offset {entry.offset} (+{entry.length} bytes) "
                f"lies outside the {len(self.blob)} bytes fetched from {self.start}. "
                f"`leaf_window_for()` spans what the root points at, in the file order "
                f"PMTiles v3 section 4 asks for -- leaf order SHOULD ascend by TileID, and "
                f"more than one level of leaf directories is discouraged. This archive "
                f"breaks one of the two; reading it needs the whole leaf section.")
        return self.blob[offset:offset + entry.length]


def leaf_window_for(root_directory, tile_id_start, tile_id_limit):
    """Find the span of the leaf section a zoom range needs.

    Prunes by exactly the rule walk_directory_tree() descends by; keep the two
    in step.

    Args:
        root_directory: The archive's deserialized root directory.
        tile_id_start: First tile id in range.
        tile_id_limit: One past the last tile id in range.

    Returns:
        `(start, length)` within the leaf section, or `(0, 0)` where the range
        needs no leaf directory at all.
    """
    start = end = None
    for index, entry in enumerate(root_directory):
        if entry.tile_id >= tile_id_limit:
            break
        if entry.run_length != 0:
            continue
        next_tile_id = (root_directory[index + 1].tile_id
                        if index + 1 < len(root_directory) else tile_id_limit)
        if next_tile_id <= tile_id_start:
            continue
        if start is None:
            start = entry.offset
        end = entry.offset + entry.length
    return (0, 0) if start is None else (start, end - start)


def tile_id_bounds(min_zoom, max_zoom):
    """The half-open tile id range a zoom range covers.

    Args:
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.

    Returns:
        `(start, limit)`, which the walk prunes against and compute_gaps()
        fills between.
    """
    return zxy_to_tileid(min_zoom, 0, 0), zxy_to_tileid(max_zoom + 1, 0, 0)


def walk_directory_tree(root_directory, leaf_window, tile_id_start, tile_id_limit):
    """Collect every entry in range, walking the tree from memory.

    Args:
        root_directory: The archive's deserialized root directory.
        leaf_window: The slice of the leaf section to descend into.
        tile_id_start: First tile id in range.
        tile_id_limit: One past the last tile id in range.

    Returns:
        The entries covering the range. The bounds prune the walk rather than
        its result, so an entry straddling a bound comes back whole.
    """
    entries = []
    progress = WalkProgress(len(leaf_window.blob), entries)
    frontier = [root_directory]

    while frontier:
        directory = frontier.pop()
        progress.popped()
        for index, entry in enumerate(directory):
            if entry.tile_id >= tile_id_limit:
                break
            if entry.run_length == 0:
                next_tile_id = (directory[index + 1].tile_id if index + 1 < len(directory)
                                 else tile_id_limit)
                if next_tile_id > tile_id_start:
                    node_bytes = leaf_window.node_bytes(entry)
                    frontier.append(deserialize_directory(node_bytes))
                    progress.decoded(node_bytes)
            elif entry.tile_id + entry.run_length > tile_id_start:
                entries.append(entry)

    return entries


def collect_entries(session, url, min_zoom, max_zoom):
    """Read an archive's directory index down to the entries a run needs.

    Two requests: the header and root directory in one, and the slice of the
    leaf section they point into in the other.

    Args:
        session: The requests session the fetches share.
        url: Absolute URL of the archive.
        min_zoom: Lowest zoom level the run walks.
        max_zoom: Highest zoom level the run walks.

    Returns:
        The archive's header, and its entries for the range in offset order.
    """
    header, root_directory = _fetch_header_and_root(session, url)
    tile_id_start, tile_id_limit = tile_id_bounds(min_zoom, max_zoom)
    leaf_window = _fetch_leaf_window(
        session, url, header, *leaf_window_for(root_directory, tile_id_start, tile_id_limit))

    print(f"starting decode ({header['tile_entries_count']} entries expected)", file=sys.stderr)
    entries = walk_directory_tree(root_directory, leaf_window, tile_id_start, tile_id_limit)
    entries.sort(key=lambda entry: entry.offset)
    return header, entries


def _fetch_header_and_root(session, url):
    """Fetch the archive's header and root directory in one request.

    Args:
        session: The requests session the fetches share.
        url: Absolute URL of the archive.

    Returns:
        The parsed header, and the deserialized root directory.

    Raises:
        RuntimeError: If the root runs past the prefix PMTiles v3 section 4
            requires header and root to fit inside.
    """
    prefix = fetch_range(session, url, 0, HEADER_AND_ROOT_PREFIX_LENGTH,
                         retry_label=RETRY_LABEL)
    header = deserialize_header(prefix[:PMTILES_HEADER_LENGTH])

    root_start, root_length = header["root_offset"], header["root_length"]
    if root_start + root_length > len(prefix):
        raise RuntimeError(
            f"root directory runs to byte {root_start + root_length}, past the "
            f"{len(prefix)} fetched: PMTiles v3 section 4 requires header plus root "
            f"inside the first {HEADER_AND_ROOT_PREFIX_LENGTH} bytes")
    return header, deserialize_directory(prefix[root_start:root_start + root_length])


def _fetch_leaf_window(session, url, header, window_start, window_length):
    """Fetch the slice of the leaf section the walk will descend into.

    Args:
        session: The requests session the fetches share.
        url: Absolute URL of the archive.
        header: The archive's parsed header.
        window_start: Offset within the leaf section to start at.
        window_length: How many bytes to fetch, or 0 for none.

    Returns:
        That slice as a LeafWindow, empty where nothing was needed.
    """
    if window_length == 0:
        return LeafWindow(b"", 0)

    print(f"starting download ({window_length} bytes of leaf directories, "
          f"{header['tile_entries_count']} entries in the archive)", file=sys.stderr)
    blob = fetch_range(
        session, url, header["leaf_directory_offset"] + window_start, window_length,
        retry_label=RETRY_LABEL,
        on_chunk=DownloadProgress(window_length, LOG_INTERVAL, "directory index").update)
    return LeafWindow(blob, window_start)
