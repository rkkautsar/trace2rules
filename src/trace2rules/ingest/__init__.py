"""Session ingestion and source adapters."""

from .normalize import (
    DEFAULT_REPOS,
    OPEN_SWE_FILES,
    OPEN_SWE_SOURCE,
    SOURCE,
    IngestStats,
    normalize_path,
)
from .service import ingest
from .writer import write_sessions

__all__ = [
    "DEFAULT_REPOS",
    "IngestStats",
    "OPEN_SWE_FILES",
    "OPEN_SWE_SOURCE",
    "SOURCE",
    "ingest",
    "normalize_path",
    "write_sessions",
]
