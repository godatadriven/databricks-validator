"""Behavioural tests for the merged `databricks-validator` command.

What is specific to it is that one run can cover more than one host format: the source is
picked per file, each kind is checked with its own config, and the two together still add
up to one exit status.
"""

from __future__ import annotations

import shutil

from tests.conftest import FIXTURES


def test_a_dashboard_and_a_python_file_are_checked_in_one_run(run_unified, py_file, tmp_path):
    dashboard = tmp_path / "dash.lvdash.json"
    shutil.copy(FIXTURES / "clean.lvdash.json", dashboard)
    script = py_file('spark.sql("SELECT 1 AS a")\n')

    result = run_unified(str(dashboard), str(script))
    assert result.returncode == 0
    assert result.snippets("query") == 1
    assert result.snippets("expression") == 2
    assert result.snippets("pyspark") == 1


def test_each_kind_is_checked_with_its_own_config(run_unified, py_file, tmp_path):
    dashboard = tmp_path / "dash.lvdash.json"
    shutil.copy(FIXTURES / "clean.lvdash.json", dashboard)
    script = py_file('spark.sql("SELECT 1 AS a")\n')

    result = run_unified(str(dashboard), str(script))
    for kind in ("query", "expression", "pyspark"):
        assert f"{kind} snippet(s) with " in result.output


def test_a_failure_in_either_format_fails_the_run(run_unified, py_file, tmp_path):
    dashboard = tmp_path / "dash.lvdash.json"
    shutil.copy(FIXTURES / "clean.lvdash.json", dashboard)
    script = py_file('spark.sql("SELECT (1")\n')

    result = run_unified(str(dashboard), str(script))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_a_file_no_source_recognises_is_reported(run_unified, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello\n")
    result = run_unified(str(other))
    assert result.returncode == 1
    assert "no source knows how to read this file" in result.output


def test_no_files_is_a_usage_error(run_unified):
    assert run_unified().returncode == 2


def test_help_succeeds(run_unified):
    result = run_unified("--help")
    assert result.returncode == 0
    assert "Usage: databricks-validator" in result.output


# --- fixing across sources --------------------------------------------------------------


def test_fix_rewrites_the_python_and_leaves_the_dashboard_alone(run_unified, py_file, tmp_path):
    dashboard = tmp_path / "dash.lvdash.json"
    shutil.copy(FIXTURES / "clean.lvdash.json", dashboard)
    before = dashboard.read_text()
    script = py_file('spark.sql("SELECT 1  AS a")\n')

    result = run_unified("--fix", str(dashboard), str(script))
    assert script.read_text() == 'spark.sql("SELECT 1 AS a")\n'
    assert dashboard.read_text() == before
    assert result.returncode == 0


# The dashboard source cannot write back yet, so a dashboard that sqlfluff would have
# changed is reported rather than silently passed over.
def test_fix_says_so_when_a_dashboard_needs_changing(run_unified, tmp_path):
    dashboard = tmp_path / "messy.lvdash.json"
    shutil.copy(FIXTURES / "messy-layout.lvdash.json", dashboard)

    result = run_unified("--fix", "--config", str(FIXTURES / "strict.sqlfluff"), str(dashboard))
    assert result.returncode == 1
    assert "cannot write it back yet" in result.output
