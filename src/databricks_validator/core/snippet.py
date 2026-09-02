"""The unit of work every source produces and every check consumes.

A source knows how to find SQL inside one kind of host file — a Lakeview dashboard's json,
a python module's ``spark.sql`` calls — and nothing else. Everything downstream of that
(scratch files, invoking sqlfluff, mapping violations back, applying fixes) works on
``Snippet`` alone, so adding a third host format is a new module under ``sources/`` rather
than a change to the pipeline.

The two host formats supported today can supply very different amounts of provenance, and
the contract is shaped so that neither is penalised for it:

``origin``
    Always present. A human-readable label for where the SQL lives — a json path such as
    ``datasets[pipeline_runs].queryLines``, or ``spark.sql`` for a python call. This is what
    a reader needs when there is no position to point at.

``base_line`` / ``base_col``
    Optional. Where the SQL starts in the host file, when the source can say. A python
    source gets this from the ast for free; a dashboard source needs a position-tracking
    json reader to supply it, and reports without it until then.

``rewrite``
    Optional. A callable that puts fixed SQL back into the host file. A source that cannot
    write back leaves it None and is lint-only; ``--fix`` then says so rather than
    pretending to have fixed something.

Read-only provenance and write-back are deliberately separate. A source can report
positions without being fixable, and the reverse, and neither is required to be a snippet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

# Kinds. A kind decides which sqlfluff config a snippet is checked with and which
# --<kind>-mode flag turns it off, so it is a checking policy rather than a source label:
# two sources may well produce the same kind.
#
# 'pyspark' rather than 'spark_sql' because the thing that distinguishes it is that the SQL
# was embedded in python source, not which engine runs it. SQL lifted out of a .sql file or
# a notebook cell later is a different kind again, and can pick its own config without
# disturbing this one.
QUERY = "query"
EXPRESSION = "expression"
PYSPARK = "pyspark"

KINDS = (QUERY, EXPRESSION, PYSPARK)


@dataclass
class Snippet:
    """One piece of SQL, and everything known about where it came from."""

    seq: int
    """Position in the run, numbered from one. Also orders the scratch filenames."""

    kind: str
    """One of KINDS. Selects the sqlfluff config and the mode flag."""

    origin: str
    """Human-readable label for the place in the host file the SQL was found."""

    sql: str
    """The SQL itself, as it will be linted, without any wrapper."""

    source: Path
    """The host file."""

    lint_prefix: str = ""
    """Text prepended when writing the scratch file, to make a fragment parsable.

    A widget expression is not a statement, so it is checked as ``SELECT <expression>``.
    The prefix is not part of the SQL: it is stripped again before a fix is written back,
    and its width is subtracted from columns reported on the snippet's first line.
    """

    base_line: int | None = None
    """1-based line in the host file that the snippet's first line sits on, if known."""

    base_col: int | None = None
    """0-based column in the host file that the snippet's first line starts at, if known."""

    skip: bool = False
    """Set when the snippet asked not to be checked, e.g. an inline sqlfluff directive."""

    skip_reason: str = ""

    rewrite: Callable[[str], None] | None = field(default=None, repr=False)
    """Put fixed SQL back into the host file, or None when the source is lint-only.

    Called with the SQL only — never with ``lint_prefix`` still attached. Sources buffer
    the edits and flush them per file, so calling this does not necessarily touch the disk
    straight away.

    Raises ValueError when this particular snippet cannot be written back after all —
    fixed SQL that no longer fits the literal that held it, say. That is reported and the
    snippet left alone; it is not a failure of the run.
    """

    def lint_text(self) -> str:
        """What gets written to the scratch file.

        sqlfluff is happier with a trailing newline, and several rules report against the
        final line, so one is added when the snippet does not already end in one.
        """
        text = f"{self.lint_prefix}{self.sql}"
        return text if text.endswith("\n") else f"{text}\n"

    def strip_prefix(self, fixed: str) -> str:
        """Take ``lint_prefix`` back off SQL that came out of ``sqlfluff fix``.

        sqlfluff may have reformatted the wrapper along with everything else — ``SELECT``
        can come back lowercased, or with the expression moved onto its own line — so this
        matches case-insensitively across whitespace rather than by length.
        """
        if not self.lint_prefix:
            return fixed
        head = self.lint_prefix.strip()
        candidate = fixed.lstrip()
        if candidate[: len(head)].casefold() == head.casefold():
            return candidate[len(head) :].strip()
        # The wrapper is gone or unrecognisable; refusing to guess is better than handing
        # back a fragment with half a SELECT still attached.
        raise ValueError(f"cannot remove {self.lint_prefix!r} wrapper from fixed SQL")

    def host_position(self, line: int, col: int) -> tuple[int, int] | None:
        """Map a position inside the snippet to one in the host file, when that is possible.

        ``line`` and ``col`` are 1-based, as sqlfluff reports them. Returns None when the
        source supplied no position, which is the signal to fall back to ``origin``.
        """
        if self.base_line is None:
            return None

        host_line = self.base_line + (line - 1)
        if line == 1:
            # Only the first line shares a line with whatever preceded the snippet, so only
            # it carries the host's starting column and the width of the lint prefix.
            host_col = col - len(self.lint_prefix)
            if self.base_col is not None:
                host_col += self.base_col
            return host_line, max(host_col, 1)
        return host_line, col

    def where(self, line: int | None = None, col: int | None = None) -> str:
        """The label a violation is reported against.

        With a position the source could supply, that is the ordinary
        ``path:line:col`` an editor can jump to. Without one it is the path and the
        origin, which is as precise as the host format allows.
        """
        if line is not None and col is not None:
            position = self.host_position(line, col)
            if position is not None:
                return f"{self.source}:{position[0]}:{position[1]}"
        return f"{self.source}: {self.origin}"


def no_flush() -> bool:
    """The flush of a source that cannot write anything back."""
    return False


class Extraction(NamedTuple):
    """One file's worth of snippets, and the way to write fixes for them back.

    Fixing is buffered rather than applied snippet by snippet: several snippets usually
    share a host file and their edits shift each other's offsets, so a source collects the
    rewrites it is handed and applies them together here.
    """

    snippets: list[Snippet]
    flush: Callable[[], bool] = no_flush
    """Apply every rewrite buffered for this file. True when the file was changed.

    Always safe to call: a lint-only source, or one that was handed no rewrites, does
    nothing and returns False.
    """
