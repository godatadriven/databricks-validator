"""One run: extract, check each kind, report.

Snippets are grouped by kind and each group is linted in a single sqlfluff invocation with
its own config. A dashboard with fifty widgets otherwise pays sqlfluff's start-up cost
fifty times over, and a widget expression genuinely needs different rules from a dataset
query, so the grouping earns its keep twice.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from databricks_validator.core import report
from databricks_validator.core.engine import Scratch
from databricks_validator.core.errors import UsageError
from databricks_validator.core.snippet import Extraction, Snippet
from databricks_validator.core.sqlfluff import run_fix, run_lint

LINT = "lint"
OFF = "off"


@dataclass
class KindPolicy:
    """How one kind of snippet is checked."""

    mode: str
    config: str


class Runner:
    """Checks a set of snippets and reports the result on one stream."""

    def __init__(
        self,
        policies: dict[str, KindPolicy],
        dialect: str,
        sqlfluff_args: list[str],
        work: Path,
        stream: TextIO,
        verbose: bool = False,
    ) -> None:
        self.policies = policies
        self.dialect = dialect
        self.sqlfluff_args = sqlfluff_args
        self.work = work
        self.stream = stream
        self.verbose = verbose

    def say(self, message: str) -> None:
        self.stream.write(message)
        self.stream.flush()

    def note(self, message: str) -> None:
        """Progress detail, suppressed unless --verbose."""
        if self.verbose:
            self.say(message)

    # --- linting ---------------------------------------------------------------------

    def lint(self, snippets: Sequence[Snippet]) -> int:
        """Check every snippet. Returns 0 when clean, 1 when anything was reported."""
        actionable = self._actionable(snippets)
        if not actionable:
            self.say("No SQL snippets found.\n")
            return 0

        scratch = Scratch(self.work / "lint")
        scratch.write_all(actionable)

        status = 0
        for kind in scratch.kinds():
            policy = self._policy(kind)
            if policy.mode == OFF:
                self.say(f"Skipping {kind} snippets (mode=off)\n")
                continue

            mapping = scratch.mapping(kind)
            self.say(f"Linting {len(mapping)} {kind} snippet(s) with {policy.config}\n")

            outcome = run_lint(
                scratch.directory(kind),
                mapping,
                self.dialect,
                policy.config,
                self.sqlfluff_args,
            )
            if outcome.failed:
                self.say(f"sqlfluff could not check the {kind} snippets:\n{outcome.raw}\n")
                status = 1
                continue
            if outcome.violations:
                self.say(report.render(outcome.violations))
                status = 1

        return status

    # --- fixing ----------------------------------------------------------------------

    def fix(self, extractions: Sequence[Extraction]) -> int:
        """Rewrite what sqlfluff can fix, then report whatever is left.

        Fixing happens per kind, exactly as linting does, and the flushes run once at the
        end so a file holding several snippets is written a single time.
        """
        snippets = [snippet for extraction in extractions for snippet in extraction.snippets]
        actionable = self._actionable(snippets)
        if not actionable:
            self.say("No SQL snippets found.\n")
            return 0

        scratch = Scratch(self.work / "fix")
        scratch.write_all(actionable)

        status = 0
        for kind in scratch.kinds():
            policy = self._policy(kind)
            if policy.mode == OFF:
                self.say(f"Skipping {kind} snippets (mode=off)\n")
                continue

            mapping = scratch.mapping(kind)
            self.say(f"Fixing {len(mapping)} {kind} snippet(s) with {policy.config}\n")

            outcome = run_fix(
                scratch.directory(kind), self.dialect, policy.config, self.sqlfluff_args
            )
            if outcome.failed:
                self.say(f"sqlfluff could not fix the {kind} snippets:\n{outcome.raw}\n")
                status = 1
                continue

            status |= self._collect_fixes(scratch, kind, mapping)

        changed = [extraction for extraction in extractions if extraction.flush()]
        self.note(f"Rewrote {len(changed)} file(s).\n")
        return status

    def _collect_fixes(self, scratch: Scratch, kind: str, mapping: dict[str, Snippet]) -> int:
        """Hand every changed snippet back to its source, or say why it was left alone."""
        status = 0
        for name, snippet in mapping.items():
            fixed = scratch.read_back(kind, name)
            # The scratch file always ends in a newline, whether or not the snippet did, so
            # the comparison ignores trailing ones. Without that a dashboard query stored
            # one string per line — every one of which carries its own newline — looks
            # changed on every run and is reported as unfixable when nothing happened.
            if fixed == snippet.sql.rstrip("\n"):
                continue

            if snippet.rewrite is None:
                self.say(
                    f"{snippet.where()}: sqlfluff can fix this, but the "
                    f"{kind} source cannot write it back yet\n"
                )
                status = 1
                continue

            try:
                snippet.rewrite(fixed)
            except ValueError as error:
                self.say(f"{snippet.where()}: not rewritten ({error})\n")
                status = 1
                continue

            self.note(f"  Fixed  {snippet.where()}\n")

        return status

    # --- shared ----------------------------------------------------------------------

    def _actionable(self, snippets: Sequence[Snippet]) -> list[Snippet]:
        kept = []
        for snippet in snippets:
            if snippet.skip:
                self.say(f"{snippet.where()}: skipped ({snippet.skip_reason})\n")
                continue
            kept.append(snippet)
        return kept

    def _policy(self, kind: str) -> KindPolicy:
        try:
            policy = self.policies[kind]
        except KeyError:  # pragma: no cover - a source producing an unregistered kind
            raise UsageError(f"no configuration for snippet kind '{kind}'") from None

        if policy.mode not in (LINT, OFF):
            raise UsageError(
                f"unsupported mode '{policy.mode}' for {kind} snippets, "
                "expected 'lint' or 'off'"
            )
        if policy.mode == LINT and not _readable(policy.config):
            raise UsageError(f"sqlfluff config not found at {policy.config}")
        return policy


def _readable(config: str) -> bool:
    return os.path.isfile(config) and os.access(config, os.R_OK)
