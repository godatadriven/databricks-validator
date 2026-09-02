"""Unit tests for the contract every source and every check share.

The behavioural suites prove the two host formats end to end. These pin the pieces in
between, which is where a third source would plug in: what a snippet promises, how a
position inside a snippet becomes a position in the host file, and what the report does
with a source that cannot supply one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from databricks_validator.core.config import configures_sqlfluff, resolve_config
from databricks_validator.core.engine import Scratch, safe_name
from databricks_validator.core.report import render
from databricks_validator.core.snippet import EXPRESSION, PYSPARK, QUERY, Snippet
from databricks_validator.core.sqlfluff import Violation


def snippet(
    seq: int = 1,
    kind: str = QUERY,
    origin: str = "datasets[runs].queryLines",
    sql: str = "SELECT 1",
    source: Path = Path("dash.lvdash.json"),
    lint_prefix: str = "",
    base_line: int | None = None,
    base_col: int | None = None,
) -> Snippet:
    """A snippet with everything defaulted, so each test names only what it is about."""
    return Snippet(
        seq=seq,
        kind=kind,
        origin=origin,
        sql=sql,
        source=source,
        lint_prefix=lint_prefix,
        base_line=base_line,
        base_col=base_col,
    )


# --- what a snippet hands to sqlfluff ---------------------------------------------------


def test_the_scratch_text_always_ends_in_a_newline():
    assert snippet(sql="SELECT 1").lint_text() == "SELECT 1\n"
    assert snippet(sql="SELECT 1\n").lint_text() == "SELECT 1\n"


def test_a_prefix_is_prepended_for_the_check_only():
    fragment = snippet(kind=EXPRESSION, sql="COUNT(x)", lint_prefix="SELECT ")
    assert fragment.lint_text() == "SELECT COUNT(x)\n"
    assert fragment.sql == "COUNT(x)"


# sqlfluff reformats the wrapper along with everything else, so it does not necessarily
# come back the width or the case it went out as.
@pytest.mark.parametrize(
    ("fixed", "expected"),
    [
        ("SELECT COUNT(x)", "COUNT(x)"),
        ("select count(x)", "count(x)"),
        ("SELECT\n    COUNT(x)", "COUNT(x)"),
        ("  SELECT COUNT(x)  ", "COUNT(x)"),
    ],
)
def test_the_prefix_comes_back_off_however_sqlfluff_reformatted_it(fixed, expected):
    fragment = snippet(kind=EXPRESSION, sql="COUNT(x)", lint_prefix="SELECT ")
    assert fragment.strip_prefix(fixed) == expected


def test_sql_that_lost_its_wrapper_is_refused_rather_than_guessed_at():
    fragment = snippet(kind=EXPRESSION, lint_prefix="SELECT ")
    with pytest.raises(ValueError, match="cannot remove"):
        fragment.strip_prefix("COUNT(x)")


# --- mapping a position back ------------------------------------------------------------


def test_a_source_with_no_position_reports_its_origin():
    assert snippet().host_position(1, 5) is None
    assert snippet().where(1, 5) == "dash.lvdash.json: datasets[runs].queryLines"


def test_a_position_is_offset_by_the_line_the_snippet_starts_on():
    located = snippet(kind=PYSPARK, source=Path("job.py"), base_line=10)
    assert located.host_position(1, 5) == (10, 5)
    assert located.host_position(3, 5) == (12, 5)
    assert located.where(3, 5) == "job.py:12:5"


def test_only_the_first_line_carries_the_starting_column():
    located = snippet(base_line=10, base_col=4)
    assert located.host_position(1, 5) == (10, 9)
    # The second line begins at column one of its own line, wherever the snippet started.
    assert located.host_position(2, 5) == (11, 5)


def test_the_wrapper_width_is_taken_off_the_first_line():
    fragment = snippet(kind=EXPRESSION, lint_prefix="SELECT ", base_line=3)
    # Column 8 of "SELECT COUNT(x)" is the C of COUNT, which is column 1 of the expression.
    assert fragment.host_position(1, 8) == (3, 1)


def test_a_column_can_never_be_pushed_below_one():
    fragment = snippet(kind=EXPRESSION, lint_prefix="SELECT ", base_line=3)
    assert fragment.host_position(1, 2) == (3, 1)


# --- the report ---------------------------------------------------------------------------


def test_nothing_is_reported_when_there_is_nothing_to_report():
    assert render([]) == ""


def test_a_snippet_without_positions_is_reported_against_its_origin():
    output = render([Violation(snippet(), 1, 13, "PRS", "Couldn't find closing bracket.")])
    assert output.splitlines() == [
        "== [dash.lvdash.json: datasets[runs].queryLines] FAIL",
        "L:   1 | P:  13 |   PRS | Couldn't find closing bracket.",
    ]


def test_a_snippet_with_positions_is_reported_at_a_place_an_editor_can_open():
    located = snippet(kind=PYSPARK, origin="spark.sql", source=Path("job.py"), base_line=10)
    output = render([Violation(located, 2, 5, "LT09", "Select targets.")])
    assert output.splitlines() == [
        "== [job.py: spark.sql] FAIL",
        "job.py:11:5 |  LT09 | Select targets.",
    ]


def test_violations_are_grouped_by_snippet_in_the_order_they_were_found():
    later = snippet(seq=2, origin="b")
    earlier = snippet(seq=1, origin="a")
    # Reported in the other order, to prove the sequence number is what decides.
    output = render([Violation(later, 1, 1, "X", "x"), Violation(earlier, 1, 1, "Y", "y")])
    assert output.index(": a]") < output.index(": b]")


def test_several_violations_on_one_snippet_share_a_header():
    only = snippet()
    output = render([Violation(only, 2, 1, "B", "b"), Violation(only, 1, 1, "A", "a")])
    assert output.count("FAIL") == 1
    # Ordered by position, not by the order sqlfluff happened to report them.
    assert output.index("| a") < output.index("| b")


# --- scratch files ------------------------------------------------------------------------


def test_a_snippet_is_written_under_a_directory_named_for_its_kind(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.write(snippet(sql="SELECT 1"))
    assert scratch.kinds() == [QUERY]
    assert list((tmp_path / QUERY).glob("*.sql"))


def test_scratch_names_are_unique_across_kinds_and_files(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.write(snippet(seq=1))
    scratch.write(snippet(seq=2, kind=PYSPARK, origin="spark.sql"))
    names = [name for kind in scratch.kinds() for name in scratch.mapping(kind)]
    assert len(set(names)) == 2


def test_a_wrapper_is_taken_off_again_when_the_file_is_read_back(tmp_path):
    scratch = Scratch(tmp_path)
    fragment = snippet(kind=EXPRESSION, sql="count(x)", lint_prefix="SELECT ")
    scratch.write(fragment)
    (name,) = scratch.mapping(EXPRESSION)
    (tmp_path / EXPRESSION / name).write_text("SELECT COUNT(x)\n")
    assert scratch.read_back(EXPRESSION, name) == "COUNT(x)"


def test_safe_name_keeps_the_identifying_tail():
    assert safe_name("datasets[runs].queryLines") == "datasets_runs_.queryLines"


def test_safe_name_replaces_every_unsafe_byte():
    assert safe_name("a/b c:d") == "a_b_c_d"


def test_safe_name_is_bounded():
    assert len(safe_name("x" * 500)) == 60


def test_safe_name_survives_non_ascii():
    # The original worked on bytes, so each byte of a multi-byte character became one
    # underscore, and keeping that means a scratch name never changes shape on a rename.
    assert safe_name("café") == "caf__"


# --- config resolution ----------------------------------------------------------------------


def test_a_named_config_wins(tmp_path):
    assert resolve_config("chosen.cfg", tmp_path / "fallback.cfg") == "chosen.cfg"


def test_a_pyproject_counts_only_when_it_configures_sqlfluff(tmp_path):
    unrelated = tmp_path / "unrelated.toml"
    unrelated.write_text('[project]\nname = "x"\n\n[tool.ruff]\nline-length = 100\n')
    assert configures_sqlfluff(str(unrelated)) is False

    relevant = tmp_path / "relevant.toml"
    relevant.write_text('[project]\nname = "x"\n\n[tool.sqlfluff.core]\ndialect = "ansi"\n')
    assert configures_sqlfluff(str(relevant)) is True


def test_a_pyproject_that_cannot_be_read_is_not_a_config(tmp_path):
    assert configures_sqlfluff(str(tmp_path / "absent.toml")) is False
