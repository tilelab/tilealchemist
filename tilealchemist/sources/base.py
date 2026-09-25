"""Source contract: which PMTiles archive to walk, and what schema it is in."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from tilealchemist.schemas import TileSchema


@dataclass(frozen=True)
class ResolvedSource:
    """The archive a run will actually read.

    Attributes:
        url: Absolute URL of the PMTiles archive.
        build: Human-readable build label for logs and source.json, or "n/a"
            for a source that has no such notion.
        schema: The schema its tiles are encoded in.
    """

    url: str
    build: str  # Human-readable label for logs and source.json; "n/a" where there is none.
    schema: TileSchema


class Source(ABC):
    """What every source answers: which archive to read, and in what schema.

    Attributes:
        schema: The schema this provider publishes, for the sources that know
            it without being told.
    """

    schema: TileSchema

    @abstractmethod
    def resolve(self):
        """Returns a ResolvedSource; called once, before the directory walk."""
