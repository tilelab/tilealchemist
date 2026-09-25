"""OpenFreeMap's planet PMTiles archive; see docs/ARCHITECTURE.md."""
import re

import requests

from tilealchemist.schemas import OPENMAPTILES
from tilealchemist.sources.base import ResolvedSource, Source

FILES_URL = "https://btrfs.openfreemap.com/files.txt"
BASE_URL = "https://btrfs.openfreemap.com/"
PLANET_RE = re.compile(r"^areas/planet/(\d{8}_\d{6})_pt/(.+)$")


class OpenFreeMapSource(Source):
    """The newest fully-published OpenFreeMap planet build."""

    schema = OPENMAPTILES

    def resolve(self):
        """Pick the newest planet build that has finished converting.

        Returns:
            The chosen build as a ResolvedSource, labelled with its timestamp.

        Raises:
            RuntimeError: If no listed build carries both its `done` marker and
                its tiles.pmtiles.
        """
        response = requests.get(FILES_URL, timeout=30)
        response.raise_for_status()

        by_timestamp = {}
        for line in response.text.splitlines():
            match = PLANET_RE.match(line.strip())
            if match:
                by_timestamp.setdefault(match.group(1), set()).add(match.group(2))

        # The newest directory listed is not necessarily finished converting.
        ready = [timestamp for timestamp, files in by_timestamp.items()
                 if "done" in files and "tiles.pmtiles" in files]
        if not ready:
            raise RuntimeError(f"no fully-published planet build found in {FILES_URL}")
        latest = max(ready)
        return ResolvedSource(f"{BASE_URL}areas/planet/{latest}_pt/tiles.pmtiles", latest,
                              self.schema)
