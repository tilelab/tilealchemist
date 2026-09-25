"""A plain, fixed PMTiles URL; the one source that must be told its schema."""
from tilealchemist.sources.base import ResolvedSource, Source


class StaticUrlSource(Source):
    """A fixed archive URL, paired with the schema the caller names for it."""

    def __init__(self, url, schema):
        """Pin a source to one URL.

        Args:
            url: Absolute URL of the PMTiles archive.
            schema: The schema its tiles are encoded in, which a bare URL cannot
                say for itself.
        """
        self.url = url
        self.schema = schema

    def resolve(self):
        """Return the pinned URL, with no build index to consult.

        Returns:
            The pinned archive as a ResolvedSource, whose build label is "n/a".
        """
        return ResolvedSource(self.url, "n/a", self.schema)
