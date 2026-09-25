"""Protomaps' daily planet basemap builds; see docs/ARCHITECTURE.md."""
import requests

from tilealchemist.schemas import PROTOMAPS
from tilealchemist.sources.base import ResolvedSource, Source

BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BASE_URL = "https://build.protomaps.com/"


class ProtomapsSource(Source):
    """The newest planet basemap in the Protomaps daily build index."""

    schema = PROTOMAPS

    def resolve(self):
        """Pick the newest .pmtiles build listed in the daily index.

        Returns:
            The chosen build as a ResolvedSource, labelled with its key and
            basemap version.

        Raises:
            RuntimeError: If the index lists no .pmtiles build at all.
        """
        response = requests.get(BUILDS_URL, timeout=30)
        response.raise_for_status()

        builds = [build for build in response.json()
                  if str(build.get("key", "")).endswith(".pmtiles")]
        if not builds:
            raise RuntimeError(f"no .pmtiles build listed in {BUILDS_URL}")

        # Build date in the key, not list position: older basemap versions linger here.
        latest = max(builds, key=lambda build: build["key"])
        version = latest.get("version", "unknown")
        return ResolvedSource(BASE_URL + latest["key"],
                              f"{latest['key'].removesuffix('.pmtiles')} (basemap {version})",
                              self.schema)
