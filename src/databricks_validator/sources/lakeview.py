"""Extract every inline SQL snippet from a Databricks Lakeview dashboard (*.lvdash.json).

The dashboard json carries SQL in more than one place, so instead of hard coding the
handful of paths that today's schema uses, this walks the whole document and picks up
anything that looks like SQL. New schema versions that move a query around keep working.

Recognised shapes:
    *.queryLines   array of strings   a dataset query, split over one string per line
    *.query        string             a dataset query as a single string (older exports)
    *.expression   string             a scalar SQL expression, e.g. a widget measure

The origin is a dotted path into the document with array indices replaced by the name of
the element where the schema provides one, e.g.

    datasets[pipeline_runs].queryLines
    pages[runs].layout[3].widget.queries[main_query].query.fields[duration_seconds].expression

This is a port of the extract-sql-snippets.jq that earlier versions shelled out to, and
deliberately keeps its behaviour down to the details: which shapes are recognised, how an
array element is named, and the order snippets come out in.

Alongside the readable origin every match also carries a ``pointer``: the same path with
array elements left as indices. Nothing in the lint path uses it, because names are what a
person wants to read. It is there because it is the stable key — two widgets may share a
name, and a name may change between exports — which is what a position-tracking reader
needs to look a value's span up by.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from databricks_validator.core.snippet import EXPRESSION, QUERY, Snippet
from databricks_validator.sources.base import Extraction, SourceError, no_flush

# Which keys hold SQL, and what to call a snippet found under each. A key not listed here
# is ignored; a listed key whose value is the wrong shape (a widget's .query is an object,
# not a string) is dropped by _sql_text below.
KEY_KINDS = {"queryLines": QUERY, "query": QUERY, "expression": EXPRESSION}

# A field expression is not a statement, so it is wrapped into the smallest query that
# makes it parsable. The width of the wrapper is subtracted from columns reported on the
# snippet's first line, so nothing downstream has to know about it.
EXPRESSION_PREFIX = "SELECT "

_NON_SPACE = re.compile(r"\S")


class Found(NamedTuple):
    """One piece of SQL, and where in the dashboard json it came from."""

    seq: int
    kind: str
    origin: str
    sql: str
    pointer: tuple[str | int, ...]


def _sql_text(value: Any) -> str | None:
    """The SQL at a value: one string, or line strings joined as stored.

    The lines of a queryLines array carry their own newlines, so they are joined with
    nothing between them. Any other shape yields None, which drops the match.
    """
    if isinstance(value, list):
        return "".join(line for line in value if isinstance(line, str))
    # bool is a subclass of int rather than of str, so it falls through to None as it
    # does in jq, where `strings` passes only actual strings.
    if isinstance(value, str):
        return value
    return None


def _element_name(element: Any, index: int) -> str:
    """Name to use for an array element, falling back to its index.

    The first of .name, .widget.name and .displayName that is present wins, and it has to
    be a non-empty string to be used at all — an element carrying `"name": 3` is numbered
    rather than called "3", which is what the jq `strings` filter did.
    """
    if isinstance(element, dict):
        widget = element.get("widget")
        candidates = (
            element.get("name"),
            widget.get("name") if isinstance(widget, dict) else None,
            element.get("displayName"),
        )
        # jq's `//` takes the first alternative that is neither null nor false.
        chosen = next((c for c in candidates if c is not None and c is not False), None)
        if isinstance(chosen, str) and chosen != "":
            return chosen
    return str(index)


def _walk(
    node: Any, trail: list[str], pointer: tuple[str | int, ...]
) -> Iterator[tuple[str, str, str, tuple[str | int, ...]]]:
    """Yield every SQL-bearing key at or below node, in document order."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = [*trail, "." + key]
            here_pointer = (*pointer, key)
            kind = KEY_KINDS.get(key)
            if kind is not None:
                sql = _sql_text(value)
                if sql is not None and _NON_SPACE.search(sql):
                    # The origin is the path including the key itself. The separator
                    # leading the very first segment is not part of it.
                    yield ("".join(here)[1:], kind, sql, here_pointer)
            yield from _walk(value, here, here_pointer)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(
                value,
                [*trail, "[" + _element_name(value, index) + "]"],
                (*pointer, index),
            )


def find_sql(document: Any) -> list[Found]:
    """Every SQL snippet in a parsed dashboard document, numbered from one."""
    return [
        Found(seq=seq, kind=kind, origin=origin, sql=sql, pointer=pointer)
        for seq, (origin, kind, sql, pointer) in enumerate(_walk(document, [], ()), start=1)
    ]


def load_document(path: Path) -> Any:
    """The parsed dashboard, or a SourceError naming the file that could not be read."""
    try:
        with open(path, "rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SourceError(f"{path}: not valid json") from error

    # jq -e treats a document of null or false as a failure too, and reporting those as
    # unusable rather than as an empty dashboard is the more useful answer anyway.
    if document is None or document is False:
        raise SourceError(f"{path}: not valid json")
    return document


class LakeviewSource:
    """SQL embedded in a Databricks Lakeview dashboard export."""

    name = "lakeview"

    # Lint only for now. The json a dashboard is exported as is machine written and
    # reformatting it wholesale would produce an unreadable diff, so writing fixes back
    # means editing the string literals in place, which needs a reader that records where
    # each value sits in the raw text. Until that exists, --fix says so.
    can_fix = False

    def matches(self, path: Path) -> bool:
        return path.name.endswith(".lvdash.json")

    def extract(self, path: Path, start: int) -> Extraction:
        document = load_document(path)

        snippets = [
            Snippet(
                seq=start + found.seq - 1,
                kind=found.kind,
                origin=found.origin,
                sql=found.sql,
                source=path,
                lint_prefix=EXPRESSION_PREFIX if found.kind == EXPRESSION else "",
            )
            for found in find_sql(document)
        ]
        return Extraction(snippets, no_flush)
