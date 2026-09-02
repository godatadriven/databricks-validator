"""Turning sqlfluff's findings into something that points at the file someone edits.

Every violation sqlfluff reports is against a scratch file with a generated name, which is
of no use to anybody. Each one is relabelled twice over: the header names the place in the
host file the SQL came from, and the violation line gives a position in the host file when
the source was able to supply one.

Sources differ in how much they can say, so the violation line has two shapes:

    == [dashboards/runs.lvdash.json: datasets[broken].queryLines] FAIL
    L:   1 | P:  13 |  PRS | Couldn't find closing bracket for opening bracket.

    == [jobs/pipeline.py: spark.sql] FAIL
    jobs/pipeline.py:42:9 |  L010 | Keywords must be upper case.

The first is a position inside the snippet, which is all a plain ``json.loads`` can
justify. The second is a position in the host file, and is the form an editor can jump to.
A source earns the second shape by filling in ``Snippet.base_line``; nothing else changes.
"""

from __future__ import annotations

from databricks_validator.core.sqlfluff import Violation


def render(violations: list[Violation]) -> str:
    """The report for one kind's run, grouped by snippet in the order they were found."""
    if not violations:
        return ""

    by_snippet: dict[int, list[Violation]] = {}
    for violation in violations:
        by_snippet.setdefault(violation.snippet.seq, []).append(violation)

    blocks = []
    for seq in sorted(by_snippet):
        found = by_snippet[seq]
        snippet = found[0].snippet
        lines = [f"== [{snippet.source}: {snippet.origin}] FAIL"]
        for violation in sorted(found, key=lambda v: (v.line, v.col, v.code)):
            lines.append(_violation_line(violation))
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n"


def _violation_line(violation: Violation) -> str:
    snippet = violation.snippet
    position = snippet.host_position(violation.line, violation.col)
    code = f"{violation.code:>5}"

    if position is None:
        # No host position to offer, so the position inside the snippet is reported as
        # sqlfluff would have reported it.
        return (
            f"L: {violation.line:>3} | P: {violation.col:>3} | {code} | {violation.description}"
        )

    host_line, host_col = position
    return f"{snippet.source}:{host_line}:{host_col} | {code} | {violation.description}"
