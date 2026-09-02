"""What a source has to provide, and how one is chosen for a file.

A source turns one host file into snippets. Everything it knows that the rest of the
pipeline does not — how to parse the format, where the SQL hides in it, how to put fixed
SQL back — stays inside the source.

``Extraction`` and ``no_flush`` live in ``core.snippet`` because they are the hand-off
between a source and the pipeline rather than anything a source owns; they are re-exported
here so a source module has one place to import its contract from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from databricks_validator.core.snippet import Extraction, no_flush

__all__ = ["Extraction", "Source", "SourceError", "no_flush"]


@runtime_checkable
class Source(Protocol):
    """A host format the validator can read SQL out of."""

    name: str
    """Short identifier used in messages."""

    can_fix: bool
    """Whether snippets from this source carry a rewrite. Decides what --fix may attempt."""

    def matches(self, path: Path) -> bool:
        """Whether this source is the one for a given file, judged by name alone."""
        ...

    def extract(self, path: Path, start: int) -> Extraction:
        """Snippets from one file, numbered from ``start``.

        A file that cannot be read at all raises ``SourceError``; a file that is readable
        but holds no SQL yields no snippets, which is not an error.
        """
        ...


class SourceError(Exception):
    """The host file could not be read as its format. Reported, and fails the run."""
