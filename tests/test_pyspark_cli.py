"""Behavioural tests for the `sqlfluff-pyspark` command.

Driven as a subprocess, because the exit status and what lands on stdout and stderr are
the contract a pre-commit hook is judged by.
"""

from __future__ import annotations

import pytest

from databricks_validator.core.config import DEFAULT_PYSPARK_CONFIG

# The dialect default is the visible behaviour change from the tool this replaces, which
# passed no dialect at all and so parsed Spark SQL as ansi.
DATABRICKS_ONLY = "SELECT a FROM t VERSION AS OF 3\n"


def test_a_clean_file_passes(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT 1 AS a")\n')
    result = run_pyspark(str(path))
    assert result.returncode == 0
    assert result.snippets("pyspark") == 1


def test_a_file_without_spark_sql_is_a_no_op(run_pyspark, py_file):
    result = run_pyspark(str(py_file("x = 1\n")))
    assert result.returncode == 0
    assert "No SQL snippets found" in result.output


def test_no_files_at_all_succeeds(run_pyspark):
    # pre-commit filters the arguments itself, so being handed nothing is ordinary.
    assert run_pyspark().returncode == 0


def test_a_file_that_is_not_python_is_ignored(run_pyspark, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("spark.sql('SELECT')\n")
    result = run_pyspark(str(other))
    assert result.returncode == 0
    assert result.snippets() == 0


def test_a_file_that_does_not_parse_is_reported(run_pyspark, py_file):
    result = run_pyspark(str(py_file("def broken(\n")))
    assert result.returncode == 1


# --- violations -----------------------------------------------------------------------


def test_sql_that_does_not_parse_fails(run_pyspark, py_file):
    result = run_pyspark(str(py_file('spark.sql("SELECT (1")\n')))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_a_violation_names_the_python_file_and_line(run_pyspark, py_file):
    path = py_file('x = 1\ny = 2\nspark.sql("SELECT (1")\n')
    result = run_pyspark(str(path))
    assert result.returncode == 1
    assert f"{path}:3:" in result.output


def test_a_violation_inside_a_triple_quoted_literal_lands_on_the_right_line(
    run_pyspark, py_file
):
    path = py_file(
        '''
        spark.sql("""
            SELECT 1 AS a
            FROM (
        """)
        '''
    )
    result = run_pyspark(str(path))
    assert result.returncode == 1
    # The literal opens on line 1, so its unbalanced bracket is on line 3 of the file.
    assert f"{path}:3:" in result.output


def test_the_dialect_defaults_to_databricks(run_pyspark, py_file):
    path = py_file(f'spark.sql("""\n{DATABRICKS_ONLY}""")\n')
    assert run_pyspark(str(path)).returncode == 0


def test_the_dialect_can_be_overridden(run_pyspark, py_file):
    path = py_file(f'spark.sql("""\n{DATABRICKS_ONLY}""")\n')
    result = run_pyspark("--dialect", "ansi", str(path))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_the_bundled_config_is_used_by_default(run_pyspark, py_file):
    result = run_pyspark(str(py_file('spark.sql("SELECT 1 AS a")\n')))
    assert f"with {DEFAULT_PYSPARK_CONFIG}" in result.output


# The tool this replaces took any pyproject.toml as its sqlfluff config, so an unrelated
# one shadowed a .sqlfluff sitting beside it.
def test_a_pyproject_that_says_nothing_about_sqlfluff_is_left_alone(run_pyspark, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    (tmp_path / "job.py").write_text('spark.sql("SELECT 1 AS a")\n')
    result = run_pyspark("job.py", cwd=tmp_path)
    assert f"with {DEFAULT_PYSPARK_CONFIG}" in result.output


def test_a_repository_sqlfluff_is_used_as_the_config(run_pyspark, tmp_path):
    (tmp_path / ".sqlfluff").write_text("[sqlfluff]\ndialect = databricks\nrules = LT01\n")
    (tmp_path / "job.py").write_text('spark.sql("SELECT 1 AS a")\n')
    result = run_pyspark("job.py", cwd=tmp_path)
    assert f"with {tmp_path / '.sqlfluff'}" in result.output


# --- skipping -------------------------------------------------------------------------


def test_an_inline_directive_skips_the_snippet(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT (1 -- sqlfluff:disable")\n')
    result = run_pyspark(str(path))
    assert result.returncode == 0
    assert "skipped (inline sqlfluff directive)" in result.output


def test_pyspark_mode_off_skips_everything(run_pyspark, py_file):
    result = run_pyspark("--pyspark-mode", "off", str(py_file('spark.sql("SELECT (1")\n')))
    assert result.returncode == 0
    assert "Skipping pyspark snippets" in result.output


# --- fixing ---------------------------------------------------------------------------


def test_fix_rewrites_the_literal(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT 1  AS a")\n')
    result = run_pyspark("--fix", str(path))
    assert result.returncode == 0
    assert path.read_text() == 'spark.sql("SELECT 1 AS a")\n'


def test_fix_reports_what_it_could_not_fix(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT (1")\n')
    result = run_pyspark("--fix", str(path))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_fix_leaves_a_clean_file_alone(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT 1 AS a")\n')
    before = path.read_text()
    assert run_pyspark("--fix", str(path)).returncode == 0
    assert path.read_text() == before


def test_fix_does_not_touch_a_skipped_snippet(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT 1  AS a -- sqlfluff:disable")\n')
    before = path.read_text()
    assert run_pyspark("--fix", str(path)).returncode == 0
    assert path.read_text() == before


def test_fix_handles_several_snippets_in_one_file(run_pyspark, py_file):
    path = py_file('spark.sql("SELECT 1  AS a")\nspark.sql("SELECT 2  AS b")\n')
    assert run_pyspark("--fix", str(path)).returncode == 0
    assert path.read_text() == 'spark.sql("SELECT 1 AS a")\nspark.sql("SELECT 2 AS b")\n'


# --- receivers ------------------------------------------------------------------------


def test_a_non_session_receiver_is_not_checked(run_pyspark, py_file):
    result = run_pyspark(str(py_file('conn.sql("SELECT (1")\n')))
    assert result.returncode == 0


def test_sql_receiver_widens_what_is_checked(run_pyspark, py_file):
    path = py_file('conn.sql("SELECT (1")\n')
    result = run_pyspark("--sql-receiver", "conn", str(path))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_sql_receiver_star_matches_everything(run_pyspark, py_file):
    path = py_file('anything.sql("SELECT (1")\n')
    assert run_pyspark("--sql-receiver", "*", str(path)).returncode == 1


# --- odds and ends --------------------------------------------------------------------


def test_help_succeeds(run_pyspark):
    result = run_pyspark("--help")
    assert result.returncode == 0
    assert "Usage: sqlfluff-pyspark" in result.output


@pytest.mark.parametrize("flag", ["--nope", "--query-mode"])
def test_a_bad_command_line_is_a_usage_error(run_pyspark, flag):
    assert run_pyspark(flag).returncode == 2
