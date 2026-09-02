"""The command line.

Everything is configured through it.

The same implementation backs the unified `databricks-validator` command and the two
compatibility commands kept from the tools this one merges, which differ only in which
sources they offer and what their usage text says.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from databricks_validator.core.config import (
    DEFAULT_EXPRESSION_CONFIG,
    DEFAULT_PYSPARK_CONFIG,
    DEFAULT_QUERY_CONFIG,
    resolve_config,
)
from databricks_validator.core.errors import UsageError
from databricks_validator.core.runner import LINT, OFF, KindPolicy, Runner
from databricks_validator.core.snippet import EXPRESSION, PYSPARK, QUERY, Extraction, Snippet
from databricks_validator.sources.base import Source, SourceError
from databricks_validator.sources.lakeview import LakeviewSource
from databricks_validator.sources.pyspark import DEFAULT_RECEIVERS, PySparkSource

PROGRAM = "databricks-validator"

USAGE = """\
Usage: databricks-validator [options] <file>...

Reads SQL out of Databricks Lakeview dashboards (*.lvdash.json) and out of spark.sql()
calls in python files, and checks it with sqlfluff.

Options:
  --fix                       apply sqlfluff's fixes to the files, then report what is left
  --dialect DIALECT           sqlfluff dialect (default: databricks)
  --config FILE               sqlfluff config for dataset queries
  --expression-config FILE    sqlfluff config for widget field expressions
  --pyspark-config FILE       sqlfluff config for SQL in spark.sql() calls
  --query-mode lint|off       how to check dataset queries (default: lint)
  --expression-mode lint|off  how to check widget expressions (default: lint)
  --pyspark-mode lint|off     how to check spark.sql() strings (default: lint)
  --sql-receiver NAME         treat NAME.sql(...) as Spark SQL, repeatable ('*' for any)
  --sqlfluff-arg ARG          extra flag passed to sqlfluff, repeatable
  --keep-tmp                  leave the extracted .sql files behind for inspection
  -v, --verbose               also report which snippet came from where
  -h, --help                  show this help

Pass options through the hook's args:

  - id: databricks-validator
    args: [--dialect, sparksql, --expression-mode, 'off']
"""


@dataclass
class Profile:
    program: str
    usage: str
    sources: list[Source]

    force_single: bool = False
    """Use the one source for every argument without consulting its matches().

    The dashboard command has always parsed whatever it was given as dashboard json,
    whatever the file was called, and hook configs in the wild rely on that.
    """

    skip_unmatched: bool = False
    """Silently pass over a file no source claims, instead of failing.

    The pyspark command has always ignored non-python arguments.
    """

    allow_no_files: bool = False
    """Succeed when given nothing to check, instead of reporting a usage error.

    The pyspark command is a pre-commit hook that filters its own arguments, so being
    handed nothing is an ordinary outcome for it rather than a mistake.
    """


class Options:
    def __init__(self) -> None:
        self.dialect = "databricks"
        self.fix = False
        # lint | off, per snippet kind.
        self.modes = {QUERY: LINT, EXPRESSION: LINT, PYSPARK: LINT}
        # Print which file each snippet came from and where it was found. This is debugging
        # output: on a failing run the violations already name their origin, so it only
        # gets in the way of the thing you are trying to read.
        self.verbose = False
        # Leave the extracted .sql files behind instead of removing them on exit.
        self.keep_tmp = False
        # None means "work it out", see resolve_config.
        self.config: str | None = None
        self.expression_config: str | None = None
        self.pyspark_config: str | None = None
        self.receivers: list[str] = []
        self.sqlfluff_args: list[str] = []
        self.files: list[str] = []


# --format is how the violations are read back out of sqlfluff, so it is not something a
# caller can hand over to it as an extra argument.
_RESERVED_SQLFLUFF_ARGS = frozenset({"--format", "-f", "--ignore-local-config", "--dialect"})

_MODE_FLAGS = {
    "--query-mode": QUERY,
    "--expression-mode": EXPRESSION,
    "--pyspark-mode": PYSPARK,
}


def parse_args(argv: Sequence[str]) -> Options | None:
    # "Parse the command line, or return None when --help asked for the usage text."
    options = Options()
    index = 0

    def value_for(flag: str) -> str:
        nonlocal index
        if index + 1 >= len(argv):
            raise UsageError(f"{flag} needs a value")
        index += 1
        return argv[index]

    while index < len(argv):
        argument = argv[index]
        if argument == "--dialect":
            options.dialect = value_for(argument)
        elif argument == "--config":
            options.config = value_for(argument)
        elif argument == "--expression-config":
            options.expression_config = value_for(argument)
        elif argument == "--pyspark-config":
            options.pyspark_config = value_for(argument)
        elif argument in _MODE_FLAGS:
            options.modes[_MODE_FLAGS[argument]] = value_for(argument)
        elif argument == "--sql-receiver":
            options.receivers.append(value_for(argument))
        elif argument == "--sqlfluff-arg":
            options.sqlfluff_args.append(value_for(argument))
        elif argument == "--fix":
            options.fix = True
        elif argument == "--keep-tmp":
            options.keep_tmp = True
        elif argument in ("-v", "--verbose"):
            options.verbose = True
        elif argument in ("-h", "--help"):
            return None
        elif argument == "--":
            index += 1
            break
        elif argument.startswith("-") and argument != "-":
            raise UsageError(f"unknown option '{argument}' (try --help)")
        else:
            break
        index += 1

    options.files = list(argv[index:])
    _validate(options)
    return options


def _validate(options: Options) -> None:
    for kind, mode in options.modes.items():
        if mode not in (LINT, OFF):
            raise UsageError(
                f"unsupported mode '{mode}' for {kind} snippets, expected 'lint' or 'off'"
            )
    for argument in options.sqlfluff_args:
        if argument.split("=", 1)[0] in _RESERVED_SQLFLUFF_ARGS:
            raise UsageError(
                f"--sqlfluff-arg {argument} is not allowed: the validator sets it itself"
            )


def policies(options: Options) -> dict[str, KindPolicy]:
    """Which config each kind is checked with.

    Dataset queries and spark.sql strings are written by hand, so a repository that has its
    own sqlfluff config gets to decide how they are judged. Widget expressions are the
    exception: they are fragments the Databricks UI generates and rewrites, so a
    repository's rules would flag every one of them, and they are always checked with the
    bundled syntax-only config.
    """
    return {
        QUERY: KindPolicy(
            options.modes[QUERY], resolve_config(options.config, DEFAULT_QUERY_CONFIG)
        ),
        EXPRESSION: KindPolicy(
            options.modes[EXPRESSION],
            options.expression_config or str(DEFAULT_EXPRESSION_CONFIG),
        ),
        PYSPARK: KindPolicy(
            options.modes[PYSPARK],
            resolve_config(options.pyspark_config, DEFAULT_PYSPARK_CONFIG),
        ),
    }


@dataclass
class Extracted:
    """Everything one run pulled out of the files it was given."""

    extractions: list[Extraction] = field(default_factory=list)
    status: int = 0

    @property
    def snippets(self) -> list[Snippet]:
        return [snippet for extraction in self.extractions for snippet in extraction.snippets]


def build_sources(profile: Profile, options: Options) -> list[Source]:
    """The profile's sources, with anything the command line configures applied."""
    receivers = tuple(options.receivers) or DEFAULT_RECEIVERS
    return [
        PySparkSource(receivers) if isinstance(source, PySparkSource) else source
        for source in profile.sources
    ]


def source_for(profile: Profile, sources: list[Source], path: Path) -> Source | None:
    if profile.force_single:
        return sources[0]
    for source in sources:
        if source.matches(path):
            return source
    return None


def extract(profile: Profile, sources: list[Source], options: Options, say, note) -> Extracted:
    """Read every file given, reporting the ones that could not be read."""
    result = Extracted()
    seq = 1

    for name in options.files:
        path = Path(name)
        source = source_for(profile, sources, path)
        if source is None:
            if profile.skip_unmatched:
                continue
            say(f"{path}: no source knows how to read this file\n")
            result.status = 1
            continue

        note(f"Extracting SQL from {path}\n")
        try:
            extraction = source.extract(path, seq)
        except SourceError as error:
            sys.stderr.write(f"{error}\n")
            sys.stderr.flush()
            result.status = 1
            continue

        for snippet in extraction.snippets:
            note(f"  Found #{snippet.seq} {snippet.kind:<10} {snippet.origin}\n")
        seq += len(extraction.snippets)
        result.extractions.append(extraction)

    return result


def run(profile: Profile, options: Options, work: Path, stream: TextIO) -> int:
    sources = build_sources(profile, options)

    if options.fix:
        fixable = {source.name for source in sources if source.can_fix}
        unfixable = sorted({source.name for source in sources if not source.can_fix})
        if not fixable:
            raise UsageError(
                f"--fix is not supported: the {', '.join(unfixable)} source is lint-only"
            )

    runner = Runner(
        policies(options),
        options.dialect,
        options.sqlfluff_args,
        work,
        stream,
        options.verbose,
    )

    extracted = extract(profile, sources, options, runner.say, runner.note)
    status = extracted.status

    if not options.fix:
        return status | runner.lint(extracted.snippets)

    status |= runner.fix(extracted.extractions)

    # Re-read the files: what is reported after a fix has to be what is on disk now, not
    # what was there before it ran.
    reread = extract(profile, sources, options, runner.say, lambda _message: None)
    return status | reread.status | runner.lint(reread.snippets)


def main(
    argv: Sequence[str] | None = None,
    profile: Profile | None = None,
) -> int:
    profile = profile or Profile(
        program=PROGRAM, usage=USAGE, sources=[LakeviewSource(), PySparkSource()]
    )
    arguments = list(sys.argv[1:] if argv is None else argv)

    try:
        options = parse_args(arguments)
        if options is None:
            sys.stdout.write(profile.usage)
            return 0
        if not options.files:
            if profile.allow_no_files:
                return 0
            raise UsageError("no files given")

        work = Path(tempfile.mkdtemp())
        if options.keep_tmp:
            sys.stdout.write(f"Keeping extracted snippets in {work}\n")
        try:
            return run(profile, options, work, sys.stdout)
        finally:
            if not options.keep_tmp:
                shutil.rmtree(work, ignore_errors=True)
    except UsageError as error:
        sys.stdout.flush()
        sys.stderr.write(f"{profile.program}: {error}\n")
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    sys.exit(main())
