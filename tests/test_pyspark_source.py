"""Unit tests for the pyspark extractor.

These cover what is recognised as Spark SQL, what is deliberately not, and the literal
round-trip that `--fix` depends on: SQL goes out to sqlfluff and the replacement that comes
back has to look like the code it replaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from databricks_validator.core.snippet import PYSPARK
from databricks_validator.sources.pyspark import PySparkSource

SOURCE = PySparkSource()


def extract(py_file, content: str, source: PySparkSource = SOURCE):
    return source.extract(py_file(content), 1).snippets


# --- what counts as a spark.sql call --------------------------------------------------


def test_a_plain_literal_is_extracted(py_file):
    (snippet,) = extract(py_file, "spark.sql('SELECT 1')\n")
    assert snippet.sql == "SELECT 1"
    assert snippet.kind == PYSPARK
    assert snippet.rewrite is not None


def test_concatenated_literals_are_folded(py_file):
    (snippet,) = extract(py_file, "spark.sql('SELECT' + ' 1')\n")
    assert snippet.sql == "SELECT 1"


def test_an_fstring_is_not_extracted(py_file):
    # There is no single SQL text before the interpolation runs.
    assert extract(py_file, "value = 2\nspark.sql(f'SELECT {value}')\n") == []


def test_a_name_is_not_extracted(py_file):
    assert extract(py_file, "query = 'SELECT 1'\nspark.sql(query)\n") == []


def test_a_call_with_no_arguments_is_not_extracted(py_file):
    assert extract(py_file, "spark.sql()\n") == []


def test_an_empty_literal_is_not_extracted(py_file):
    assert extract(py_file, "spark.sql('   ')\n") == []


@pytest.mark.parametrize(
    "call",
    [
        "spark.sql('SELECT 1')",
        "self.spark.sql('SELECT 1')",
        "ctx.spark_session.sql('SELECT 1')",
        "self._spark.sql('SELECT 1')",
    ],
)
def test_a_spark_session_receiver_is_recognised(py_file, call):
    # An attribute chain is judged by its last component, and a call is unwrapped once.
    assert len(extract(py_file, f"{call}\n")) == 1


# The previous tool matched every `.sql(...)` in the file, so a database connection or a
# query builder had its arguments linted as Spark SQL.
@pytest.mark.parametrize(
    "call",
    ["conn.sql('SELECT 1')", "duckdb.sql('SELECT 1')", "self.engine.sql('SELECT 1')"],
)
def test_a_receiver_that_is_not_a_session_is_left_alone(py_file, call):
    assert extract(py_file, f"{call}\n") == []


def test_a_receiver_can_be_added(py_file):
    source = PySparkSource(receivers=("conn",))
    assert len(extract(py_file, "conn.sql('SELECT 1')\n", source)) == 1


def test_a_factory_call_is_judged_by_the_function_it_called(py_file):
    # get_spark() is not a default receiver, but naming it makes the shape reachable.
    assert extract(py_file, "get_spark().sql('SELECT 1')\n") == []
    source = PySparkSource(receivers=("get_spark",))
    assert len(extract(py_file, "get_spark().sql('SELECT 1')\n", source)) == 1


def test_every_receiver_can_be_matched_again(py_file):
    source = PySparkSource(receivers=("*",))
    assert len(extract(py_file, "anything.sql('SELECT 1')\n", source)) == 1


def test_a_method_that_is_not_called_sql_is_left_alone(py_file):
    assert extract(py_file, "spark.read('SELECT 1')\n") == []


# --- skipping and fixability ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1 -- sqlfluff: disable", "SELECT 1 -- sqlfluff:disable:all", "SELECT 1 -- noqa"],
)
def test_an_inline_directive_skips_the_snippet(py_file, sql):
    (snippet,) = extract(py_file, f"spark.sql('{sql}')\n")
    assert snippet.skip is True
    assert snippet.skip_reason


# The previous tool matched the bare substring "sqlfluff:", so both of these were skipped:
# the first is configuration sqlfluff itself acts on, and skipping it dropped the check.
@pytest.mark.parametrize(
    "sql", ["SELECT 1 -- sqlfluff:dialect:postgres", "SELECT 'sqlfluff: not a directive'"]
)
def test_something_that_is_not_a_disabling_directive_does_not_skip(py_file, sql):
    (snippet,) = extract(py_file, f'spark.sql("{sql}")\n')
    assert snippet.skip is False


# A bytes literal is not a str constant, so it never reaches the extractor at all — which
# is why nothing downstream has to have an opinion about rebuilding one.
def test_a_bytes_literal_is_not_extracted(py_file):
    assert extract(py_file, "spark.sql(b'SELECT 1')\n") == []


# --- positions ------------------------------------------------------------------------


def test_a_single_line_literal_reports_its_own_line(py_file):
    (snippet,) = extract(py_file, "x = 1\ny = 2\nspark.sql('SELECT 1')\n")
    assert snippet.base_line == 3
    assert snippet.where(1, 1).endswith(":3:1")


def test_a_triple_quoted_literal_starts_on_the_line_after_the_quotes(py_file):
    (snippet,) = extract(
        py_file,
        """
        spark.sql(\"\"\"
            SELECT 1
            FROM t
        \"\"\")
        """,
    )
    # The quotes open on line 1, so the SQL starts on line 2 and `FROM` is on line 3.
    assert snippet.base_line == 2
    assert snippet.where(2, 1).endswith(":3:1")


def test_several_snippets_in_one_file_are_numbered_in_order(py_file):
    snippets = extract(py_file, "spark.sql('SELECT 1')\nspark.sql('SELECT 2')\n")
    assert [(s.seq, s.base_line) for s in snippets] == [(1, 1), (2, 2)]


# --- rebuilding the literal -----------------------------------------------------------


def rewrite(py_file, content: str, new_sql: str) -> str:
    """Apply one rewrite to a file and hand back what it now contains."""
    path = py_file(content)
    extraction = SOURCE.extract(path, 1)
    (snippet,) = extraction.snippets
    assert snippet.rewrite is not None
    snippet.rewrite(new_sql)
    assert extraction.flush() is True
    return path.read_text()


def test_a_single_line_fix_keeps_the_original_quotes(py_file):
    assert rewrite(py_file, "spark.sql('select 1')\n", "SELECT 1") == "spark.sql('SELECT 1')\n"


def test_a_double_quoted_literal_stays_double_quoted(py_file):
    assert rewrite(py_file, 'spark.sql("select 1")\n', "SELECT 1") == 'spark.sql("SELECT 1")\n'


def test_a_raw_prefix_is_kept(py_file):
    assert (
        rewrite(py_file, "spark.sql(r'select 1')\n", "SELECT 1") == "spark.sql(r'SELECT 1')\n"
    )


def test_quoting_moves_out_of_the_way_of_the_sql(py_file):
    # The SQL now carries a single quote, so the literal cannot stay single-quoted.
    result = rewrite(py_file, "spark.sql('select a')\n", "SELECT 'a'")
    assert result == "spark.sql(\"SELECT 'a'\")\n"


def test_sql_that_no_quoting_holds_is_refused_rather_than_escaped(py_file):
    path = py_file("spark.sql('select 1')\n")
    (snippet,) = SOURCE.extract(path, 1).snippets
    assert snippet.rewrite is not None
    with pytest.raises(ValueError, match="without escaping"):
        snippet.rewrite("SELECT \"a\", 'b'")


def test_sql_that_grew_a_line_break_is_promoted_to_triple_quotes(py_file):
    result = rewrite(py_file, "spark.sql('select 1')\n", "SELECT\n    1")
    assert result.startswith('spark.sql("""\n')
    assert result.endswith('""")\n')
    assert "SELECT" in result


def test_a_promoted_literal_is_indented_to_the_call(py_file):
    result = rewrite(py_file, "def f(spark):\n    spark.sql('select 1')\n", "SELECT\n1")
    # The body and the closing quotes line up under the opening ones, rather than the
    # closing quotes being flushed to column zero.
    body = result.splitlines()
    assert body[2].startswith(" " * 14 + "SELECT")
    assert body[-1] == " " * 14 + '""")'


def test_a_multi_line_literal_keeps_its_indentation(py_file):
    result = rewrite(
        py_file,
        'def f(spark):\n    spark.sql("""\n        select 1\n        """)\n',
        "SELECT 1\nFROM t",
    )
    assert "        SELECT 1\n        FROM t\n" in result


def test_nothing_is_written_when_no_rewrite_was_handed_over(py_file):
    path = py_file("spark.sql('SELECT 1')\n")
    before = path.read_text()
    assert SOURCE.extract(path, 1).flush() is False
    assert path.read_text() == before


def test_several_rewrites_in_one_file_do_not_shift_each_other(py_file):
    path = py_file("spark.sql('select 1')\nspark.sql('select 2')\n")
    extraction = SOURCE.extract(path, 1)
    for snippet, fixed in zip(
        extraction.snippets, ["SELECT 1", "SELECT 2 FROM a_much_longer_name"], strict=True
    ):
        assert snippet.rewrite is not None
        snippet.rewrite(fixed)
    assert extraction.flush() is True
    assert path.read_text() == (
        "spark.sql('SELECT 1')\nspark.sql('SELECT 2 FROM a_much_longer_name')\n"
    )


# --- unreadable files -----------------------------------------------------------------


def test_a_file_that_does_not_parse_is_reported(py_file):
    from databricks_validator.sources.base import SourceError

    with pytest.raises(SourceError):
        extract(py_file, "def broken(\n")


def test_matches_only_python_files():
    assert SOURCE.matches(Path("a.py")) is True
    assert SOURCE.matches(Path("a.lvdash.json")) is False
