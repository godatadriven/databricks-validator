"""How to invoke sqlfluff, and what comes back when it has run."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from databricks_validator.core.errors import UsageError
from databricks_validator.core.snippet import Snippet


@dataclass
class Violation:
    """One sqlfluff finding, already tied back to the snippet it belongs to."""

    snippet: Snippet
    line: int
    col: int
    code: str
    description: str


@dataclass
class Outcome:
    """What one sqlfluff run produced.

    ``violations`` is empty and ``failed`` is set when sqlfluff itself could not run — a
    bad config, an unknown dialect, a crash. That is a different thing from a clean run and
    has to be reported differently, so the two are not collapsed into an exit status.
    """

    violations: list[Violation]
    failed: bool = False
    raw: str = ""


def sqlfluff_command() -> list[str]:
    """The argv prefix that runs sqlfluff.

    sqlfluff is a dependency of this package, so the interpreter running the validator is
    the one that has the pinned version installed — preferring ``-m`` over whatever
    ``sqlfluff`` the PATH happens to offer keeps a pre-commit hook, the image and a
    developer's shell on the same version.
    """
    if _has_sqlfluff_module():
        return [sys.executable, "-m", "sqlfluff"]
    executable = shutil.which("sqlfluff")
    if executable is None:
        raise UsageError("sqlfluff is not installed")
    return [executable]


def _has_sqlfluff_module() -> bool:
    try:
        import sqlfluff  # noqa: F401
    except ImportError:
        return False
    return True


def base_args(dialect: str, config: str, extra: list[str]) -> list[str]:
    """The flags every lint and fix run shares.

    ``--ignore-local-config`` stops sqlfluff searching its default locations on top of the
    config named here, so the file reported in the output is genuinely the only one in
    play. It does not suppress ``--config``. Without it the working directory's config is
    merged in half way: the snippets sit outside the repository, so core settings are
    picked up but the nested sections they depend on are not, which is what breaks a
    project that configures a templater. It also keeps a stray ~/.sqlfluff from changing
    results between a laptop and CI.
    """
    return [
        "--dialect",
        dialect,
        "--config",
        config,
        "--ignore-local-config",
        "--disable-progress-bar",
        *extra,
    ]


def run_lint(
    directory: Path,
    mapping: dict[str, Snippet],
    dialect: str,
    config: str,
    extra: list[str],
) -> Outcome:
    """Lint every scratch file in one directory with a single sqlfluff invocation.

    One run per kind rather than one per snippet: a dashboard with fifty widgets otherwise
    pays sqlfluff's start-up cost fifty times over.

    ``--format json`` rather than reading the human-readable output. The rendering is this
    package's job, because a violation has to be relabelled from the scratch file it was
    found in to the place in the host file someone can actually go and fix, and scraping
    that out of prose with a regex breaks silently whenever sqlfluff adjusts its layout.
    """
    command = [
        *sqlfluff_command(),
        "lint",
        *base_args(dialect, config, extra),
        "--format",
        "json",
        str(directory),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")

    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        # sqlfluff could not get as far as producing a report: an unusable config, an
        # unknown dialect, a templater that raised. Its own words are the useful thing here.
        return Outcome([], failed=True, raw=(completed.stdout + completed.stderr).strip())

    violations = []
    for entry in payload:
        snippet = mapping.get(Path(entry.get("filepath", "")).name)
        if snippet is None:
            continue
        for found in entry.get("violations", []):
            violations.append(
                Violation(
                    snippet=snippet,
                    line=found.get("start_line_no") or found.get("line_no") or 1,
                    col=found.get("start_line_pos") or found.get("line_pos") or 1,
                    code=found.get("code", ""),
                    description=found.get("description", ""),
                )
            )

    # sqlfluff exits 1 for violations and higher for its own failures. An exit above 1 with
    # nothing reported means it fell over somewhere the json did not capture.
    failed = completed.returncode > 1 and not violations
    return Outcome(violations, failed=failed, raw=(completed.stdout + completed.stderr).strip())


def run_fix(directory: Path, dialect: str, config: str, extra: list[str]) -> Outcome:
    """Rewrite every scratch file in one directory in place.

    ``sqlfluff fix`` exits 0 when it fixed everything and 1 when violations it cannot fix
    are left over — neither is a failure of the run, and the leftovers are reported by the
    lint pass that follows.
    """
    command = [
        *sqlfluff_command(),
        "fix",
        *base_args(dialect, config, extra),
        str(directory),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    raw = (completed.stdout + completed.stderr).strip()
    return Outcome([], failed=completed.returncode > 1, raw=raw)
