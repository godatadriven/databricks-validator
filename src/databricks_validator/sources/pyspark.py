"""Extract the SQL passed to ``spark.sql(...)`` out of python source.

The module is parsed with ``ast``, so this sees what the interpreter would: a call, its
receiver, and the literal handed to it. That also means every snippet arrives with real
positions attached, which is what lets a violation be reported at the line of the original
python file and a fix be written back into the literal it came from.

What is recognised:

    spark.sql("SELECT 1")            a plain string literal
    spark.sql("SELECT" + " 1")       adjacent literals folded at parse time
    self.spark.sql(\"\"\"...\"\"\")        a session reached through an attribute

What is deliberately not:

    spark.sql(f"SELECT {table}")     an f-string is not knowable before it runs
    spark.sql(query)                 a name, likewise
    conn.sql("SELECT 1")             not a Spark session, see _is_sql_call

The receiver is checked rather than matching every ``.sql(...)`` call in the file. Plenty
of libraries expose a method by that name — database connections, query builders, duckdb —
and linting their arguments as Spark SQL produces violations against code this tool has no
business judging. ``--sql-receiver`` widens the set, and ``--sql-receiver '*'`` restores
matching everything.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from databricks_validator.core.snippet import PYSPARK, Snippet
from databricks_validator.sources.base import Extraction, SourceError

# Names a Spark session is conventionally bound to. An attribute access is matched on its
# final component, so `self.spark`, `ctx.spark` and `self._spark_session` all qualify.
DEFAULT_RECEIVERS = ("spark", "spark_session", "sparkSession", "_spark", "_spark_session")

STRING_PREFIX_RE = re.compile(r'^([furbFURB]*)("""|\'\'\'|"|\')')
DEFAULT_QUOTE = '"""'

# An inline request to leave a snippet alone. sqlfluff's own directives look like
# `-- sqlfluff:disable:all`, and only the disabling ones mean "do not check this" — a
# `-- sqlfluff:dialect:postgres` is configuration that sqlfluff itself acts on, and
# skipping the snippet over it would silently drop the check. Matching the directive rather
# than the bare substring also stops a query that merely mentions the word from being
# skipped.
SKIP_DIRECTIVE = re.compile(
    r"(?:--|#|/\*)\s*(?:sqlfluff\s*:\s*(?:disable|ignore)|noqa)\b", re.I
)


@dataclass
class Literal:
    """A string literal in the source, and everything needed to rebuild it.

    Rebuilding rather than reformatting: a fix has to land back inside the python file
    looking like the code around it, so the original quote style, prefix and indentation
    are all carried through.
    """

    lineno: int
    col_offset: int
    end_lineno: int | None
    end_col_offset: int | None
    text: str
    value: str
    prefix: str
    quote: str
    indent: str
    continuation_indent: str
    leading_newline: bool
    trailing_newline: bool

    def build(self, new_sql: str) -> str:
        """The replacement literal for fixed SQL, in the style of the original.

        Raises ValueError when there is no quoting that holds the SQL without escaping it,
        which is the signal to leave the literal alone rather than write broken python.
        """
        multi_line = "\n" in new_sql or "\r" in new_sql

        quote = self.quote
        indent = self.indent
        leading_newline = self.leading_newline
        trailing_newline = self.trailing_newline

        if multi_line:
            # A single-quoted literal cannot hold newlines, so SQL that grew a line break
            # during the fix is promoted to a triple-quoted one, laid out over its own
            # lines at the call's indentation.
            indent = indent or self.continuation_indent
            leading_newline = True
            trailing_newline = True
            if len(quote) == 1:
                quote = DEFAULT_QUOTE

        quote = _quote_that_holds(new_sql, quote)

        lines = new_sql.splitlines()
        if indent and lines:
            # A blank line is left blank rather than padded out with trailing whitespace,
            # which the repository's own formatter would only strip again.
            lines = [f"{indent}{line}" if line else indent.rstrip() for line in lines]

        body = "\n".join(lines)
        if leading_newline:
            body = f"\n{body}"
        if trailing_newline:
            # The closing quotes line up with the body rather than being flushed to column
            # zero, which is what a person writing the literal by hand would have done and
            # what the repository's own formatter will not then argue with.
            body = f"{body}\n{indent}"

        return f"{self.prefix}{quote}{body}{quote}"


def _quote_that_holds(text: str, preferred: str) -> str:
    """A quoting the SQL fits inside without being escaped.

    SQL routinely carries both kinds of quote — a string literal in single quotes, an
    identifier in backticks or double quotes — so the style the literal was originally
    written in is not always still usable once sqlfluff has rewritten it. The preferred
    style is tried first, then the others of the same width, and rebuilding is refused
    rather than escaping the SQL, which would leave the python source saying something
    different from what was checked.
    """
    same_width = ('"""', "'''") if len(preferred) == 3 else ('"', "'")
    for quote in (preferred, *same_width):
        if quote not in text and not text.endswith(quote[0]) and not text.endswith("\\"):
            return quote
    raise ValueError("no python quoting holds this SQL without escaping it")


class _Visitor(ast.NodeVisitor):
    """Collects the string literals handed to a Spark session's ``sql`` method."""

    def __init__(self, source: str, receivers: tuple[str, ...]) -> None:
        self._source = source
        self._receivers = receivers
        self.found: list[tuple[str, Literal]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_sql_call(node) and node.args:
            self._collect(node.args[0])
        self.generic_visit(node)

    def _is_sql_call(self, node: ast.Call) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "sql":
            return False
        if "*" in self._receivers:
            return True
        return self._receiver_name(func.value) in self._receivers

    def _receiver_name(self, node: ast.expr) -> str | None:
        """The name the call was made on, as far as it can be read statically.

        An attribute chain is judged by its last component, so `self.spark` and
        `context.spark` both read as `spark`. A call is unwrapped once and judged by the
        function it called, so `get_spark().sql(...)` reads as `get_spark` — not a default
        receiver, but one `--sql-receiver get_spark` can add. Anything deeper is not worth
        guessing at, and `--sql-receiver '*'` is there for the cases this does not reach.
        """
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call):
            return self._receiver_name(node.func)
        return None

    def _collect(self, value: ast.expr) -> None:
        raw = _as_string(value)
        if raw is None:
            return

        sql = textwrap.dedent(raw).strip()
        if not sql:
            return

        text = ast.get_source_segment(self._source, value) or ""
        prefix, quote = _split_prefix_and_quote(text)

        literal = Literal(
            lineno=getattr(value, "lineno", 1),
            col_offset=getattr(value, "col_offset", 0),
            end_lineno=getattr(value, "end_lineno", None),
            end_col_offset=getattr(value, "end_col_offset", None),
            text=text,
            value=raw,
            prefix=prefix,
            quote=quote,
            indent=_infer_indent(raw),
            continuation_indent=" " * max(getattr(value, "col_offset", 0), 0),
            leading_newline=raw.startswith("\n"),
            trailing_newline=raw.endswith("\n"),
        )
        # Every literal that gets this far can be rebuilt: the shapes that could not be —
        # an f-string, a bytes literal — do not survive _as_string. What can still go wrong
        # is the SQL itself containing the quotes the replacement would be wrapped in, and
        # that is only knowable once the fixed SQL is in hand, so Literal.build decides it.
        self.found.append((sql, literal))


def _as_string(node: ast.AST) -> str | None:
    """The value of a literal expression, when it is knowable without running the code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.JoinedStr):
        # f-strings interpolate at runtime; there is no single SQL text to check.
        return None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _as_string(node.left)
        right = _as_string(node.right)
        if left is not None and right is not None:
            return left + right

    return None


def _split_prefix_and_quote(literal: str) -> tuple[str, str]:
    match = STRING_PREFIX_RE.match(literal)
    if not match:
        return "", DEFAULT_QUOTE
    prefix, quote = match.groups()
    return prefix or "", quote or DEFAULT_QUOTE


def _infer_indent(raw: str) -> str:
    """The indentation the SQL body was written at, taken from its first non-blank line."""
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped:
            return line[: len(line) - len(stripped)]
    return ""


def _leading_newlines(value: str) -> int:
    """How many line breaks a literal opens with, before any SQL.

    A triple-quoted literal usually starts on the line after the opening quotes, so the
    first line of SQL is that many lines below the literal itself.
    """
    count = 0
    for char in value:
        if char == "\n":
            count += 1
        elif char.isspace():
            continue
        else:
            break
    return count


@dataclass
class _Edits:
    """Replacements buffered for one file, applied together by ``flush``."""

    path: Path
    pending: list[tuple[Literal, str]] = field(default_factory=list)

    def add(self, literal: Literal, new_literal: str) -> None:
        self.pending.append((literal, new_literal))

    def flush(self) -> bool:
        if not self.pending:
            return False

        try:
            original = self.path.read_text(encoding="utf-8")
        except OSError:
            return False

        offsets = _line_offsets(original)
        edits = []
        for literal, replacement in self.pending:
            start = _offset(offsets, literal.lineno, literal.col_offset)
            if literal.end_lineno is not None and literal.end_col_offset is not None:
                end = _offset(offsets, literal.end_lineno, literal.end_col_offset)
            else:
                end = start + len(literal.text)
            edits.append((start, end, replacement))

        # Back to front, so an earlier replacement cannot shift the offsets of a later one.
        edits.sort(key=lambda item: item[0], reverse=True)

        updated = original
        for start, end, replacement in edits:
            updated = f"{updated[:start]}{replacement}{updated[end:]}"

        self.pending.clear()
        if updated == original:
            return False
        self.path.write_text(updated, encoding="utf-8")
        return True


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    running = 0
    for line in text.splitlines(keepends=True):
        running += len(line)
        offsets.append(running)
    return offsets


def _offset(offsets: list[int], lineno: int, col: int) -> int:
    index = max(lineno - 1, 0)
    if index >= len(offsets):
        return offsets[-1] if offsets else 0
    return offsets[index] + col


class PySparkSource:
    """SQL embedded in python source as arguments to a Spark session's ``sql`` method."""

    name = "pyspark"
    can_fix = True

    def __init__(self, receivers: tuple[str, ...] = DEFAULT_RECEIVERS) -> None:
        self.receivers = receivers

    def matches(self, path: Path) -> bool:
        return path.suffix == ".py"

    def extract(self, path: Path, start: int) -> Extraction:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SourceError(f"{path}: cannot be read as utf-8 python source") from error

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            raise SourceError(f"{path}: {error}") from error

        visitor = _Visitor(source, self.receivers)
        visitor.visit(tree)

        edits = _Edits(path)
        snippets = [
            _snippet(start + index, sql, literal, path, edits)
            for index, (sql, literal) in enumerate(visitor.found)
        ]
        return Extraction(snippets, edits.flush)


def _snippet(seq: int, sql: str, literal: Literal, path: Path, edits: _Edits) -> Snippet:
    # A triple-quoted literal normally opens on the line of the call and puts its first
    # line of SQL on the next one, so the newlines the value starts with are what separate
    # the literal's own line from the SQL's.
    base_line = literal.lineno + _leading_newlines(literal.value)

    snippet = Snippet(
        seq=seq,
        kind=PYSPARK,
        origin="spark.sql",
        sql=sql,
        source=path,
        base_line=base_line,
        skip=bool(SKIP_DIRECTIVE.search(sql)),
    )
    if snippet.skip:
        snippet.skip_reason = "inline sqlfluff directive"
    snippet.rewrite = lambda new_sql: edits.add(literal, literal.build(new_sql))
    return snippet
