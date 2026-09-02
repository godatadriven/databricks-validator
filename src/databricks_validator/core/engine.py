"""The scratch directory the snippets are linted from.

Snippets are written to files rather than passed as arguments, so a query of any size is
handled — a dashboard query padded out to a megabyte fails with "Argument list too long"
well before sqlfluff ever sees it. They are grouped into one directory per kind, because
each kind is linted with its own config in a single sqlfluff invocation.
"""

from __future__ import annotations

from pathlib import Path

from databricks_validator.core.snippet import Snippet

# Bytes kept when turning a snippet's origin into a filename; everything else, including
# every byte above ASCII, becomes an underscore.
_FILENAME_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def safe_name(origin: str) -> str:
    """Turn a snippet's origin into something that survives being used as a filename.

    The tail is kept because that is the part that identifies the widget and the field.
    """
    raw = origin.encode("utf-8", "surrogateescape")
    cleaned = bytes(byte if byte in _FILENAME_SAFE else ord("_") for byte in raw)
    return cleaned[-60:].decode("ascii")


class Scratch:
    """Snippets on disk, arranged by kind."""

    def __init__(self, work: Path) -> None:
        self.work = work
        # kind -> scratch basename -> snippet. The basename is what sqlfluff reports
        # against, and is unique across the whole run because it carries the sequence
        # number, so a dashboard and a python file checked together cannot collide.
        self._by_kind: dict[str, dict[str, Snippet]] = {}

    def write(self, snippet: Snippet) -> None:
        directory = self.work / snippet.kind
        directory.mkdir(parents=True, exist_ok=True)

        name = f"{snippet.seq:04d}_{safe_name(snippet.origin)}.sql"
        (directory / name).write_text(snippet.lint_text(), encoding="utf-8")
        self._by_kind.setdefault(snippet.kind, {})[name] = snippet

    def write_all(self, snippets: list[Snippet]) -> None:
        for snippet in snippets:
            self.write(snippet)

    def kinds(self) -> list[str]:
        return list(self._by_kind)

    def mapping(self, kind: str) -> dict[str, Snippet]:
        return self._by_kind.get(kind, {})

    def directory(self, kind: str) -> Path:
        return self.work / kind

    def read_back(self, kind: str, name: str) -> str:
        """A scratch file after ``sqlfluff fix`` has rewritten it, without the wrapper."""
        text = (self.directory(kind) / name).read_text(encoding="utf-8").rstrip("\n")
        return self.mapping(kind)[name].strip_prefix(text)

    def empty(self) -> bool:
        return not self._by_kind
