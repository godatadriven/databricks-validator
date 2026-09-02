"""The `sqlfluff-pyspark` command, as the tool of that name shipped it.

Kept so that a repository pinning either of the published hook ids keeps working across the
merge. It is the unified command restricted to the python source, and it passes over
arguments that are not python files rather than failing on them, as it always has.

Two things behave differently from the original, both deliberate and both documented in the
README's migration notes: the dialect defaults to `databricks` instead of falling through
to sqlfluff's `ansi`, and only calls on something that looks like a Spark session are
picked up rather than every `.sql(...)` in the file.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from databricks_validator import cli
from databricks_validator.sources.pyspark import PySparkSource

PROGRAM = "sqlfluff-pyspark"

USAGE = """\
Usage: sqlfluff-pyspark [options] <file.py>...

Options:
  --fix                       apply sqlfluff's fixes to the literals, then report the rest
  --dialect DIALECT           sqlfluff dialect (default: databricks)
  --pyspark-config FILE       sqlfluff config for the extracted SQL
  --pyspark-mode lint|off     how to check spark.sql() strings (default: lint)
  --sql-receiver NAME         treat NAME.sql(...) as Spark SQL, repeatable ('*' for any)
  --sqlfluff-arg ARG          extra flag passed to sqlfluff, repeatable
  --keep-tmp                  leave the extracted .sql files behind for inspection
  -v, --verbose               also report which snippet came from where
  -h, --help                  show this help

Arguments that are not python files are ignored.
"""

PROFILE = cli.Profile(
    program=PROGRAM,
    usage=USAGE,
    sources=[PySparkSource()],
    skip_unmatched=True,
    allow_no_files=True,
)


def main(argv: Sequence[str] | None = None) -> int:
    return cli.main(argv, PROFILE)


if __name__ == "__main__":
    sys.exit(main())
