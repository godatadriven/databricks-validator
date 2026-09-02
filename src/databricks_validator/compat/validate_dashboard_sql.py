"""The `validate-dashboard-sql` command, as databricks-dashboard-validator shipped it.

Kept so that a repository pinning one of the published hook ids keeps working across the
merge. It is the unified command restricted to the Lakeview source, with the flags, the
usage text and the exit statuses it had before.

Every argument is read as dashboard json whatever it is called, which is what this command
has always done and what hook configs overriding `files:` depend on.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from databricks_validator import cli
from databricks_validator.sources.lakeview import LakeviewSource

PROGRAM = "validate-dashboard-sql"

USAGE = """\
Usage: validate-dashboard-sql [options] <dashboard.lvdash.json>...

Options:
  --dialect DIALECT           sqlfluff dialect (default: databricks)
  --config FILE               sqlfluff config for dataset queries
  --expression-config FILE    sqlfluff config for widget field expressions
  --query-mode lint|off       how to check dataset queries (default: lint)
  --expression-mode lint|off  how to check widget expressions (default: lint)
  --sqlfluff-arg ARG          extra flag passed to sqlfluff, repeatable
  --keep-tmp                  leave the extracted .sql files behind for inspection
  -v, --verbose               also report which snippet came from where in the json
  -h, --help                  show this help

There are no environment variable equivalents. Under pre-commit the command line is the
only channel that works, because pre-commit does not forward the environment into a
`language: docker` hook, so anything else would apply to a local run and silently not to a
containerised one. Pass options through the hook's args:

  - id: validate-dashboard-sql
    args: [--dialect, sparksql, --expression-mode, 'off']

Note that pre-commit's own -v does not reach this script: pre-commit captures hook output
and shows it only when the hook fails or when -v is given, but the hook cannot tell which
mode it is in. To get the snippet inventory under pre-commit, put --verbose in the hook's
args.
"""

PROFILE = cli.Profile(
    program=PROGRAM,
    usage=USAGE,
    sources=[LakeviewSource()],
    force_single=True,
)


def main(argv: Sequence[str] | None = None) -> int:
    return cli.main(argv, PROFILE)


if __name__ == "__main__":
    sys.exit(main())
