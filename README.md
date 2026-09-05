# databricks-validator

Pre-commit hooks that pull the SQL out of the files it is buried in and check it with
[sqlfluff](https://sqlfluff.com/).

Two kinds of file are supported:

- **Databricks Lakeview dashboards** (`*.lvdash.json`). A dashboard is checked into git as
  one machine-written json blob with the SQL somewhere inside it. Nothing in a normal
  review catches a broken query in there — you find out when the dashboard is published and
  a widget renders an error.
- **Python source** (`*.py`). SQL handed to `spark.sql("...")` is a string as far as every
  other tool is concerned, so a missing bracket survives every linter in the repository and
  fails at runtime.

Both go through the same pipeline — extract, lint with sqlfluff, report against the place
in the original file.

This repository merges two tools that did one format each:
[databricks-dashboard-validator](https://github.com/krisgeus/databricks-dashboard-validator)
and [sqlfluff-pyspark](https://github.com/dan1elt0m/sqlfluff-pyspark). Both of their
commands and all of their hook ids still work — see [Migrating](#migrating).

## What gets extracted

### From a dashboard

The dashboard json carries SQL in more than one place, so rather than hard coding the paths
that today's schema happens to use, the extractor walks the whole document and picks up
anything that looks like SQL. A schema version that moves a query somewhere else keeps
working.

| Shape in the json | Kind | What it is |
| --- | --- | --- |
| `*.queryLines` (array of strings) | `query` | A dataset query, stored one string per line |
| `*.query` (string) | `query` | A dataset query as a single string, as older exports write it |
| `*.expression` (string) | `expression` | A scalar SQL expression, such as a widget measure or a filter field |

#### Extraction example

In practice that means the dataset queries under `datasets[]` plus every field expression
under `pages[].layout[].widget.queries[].query.fields[]` for `pipeline_runs` in
`examples/`, one query and twenty seven expressions.

### From python

| Shape in the source | Kind | Extracted |
| --- | --- | --- |
| `spark.sql("SELECT 1")` | `pyspark` | yes |
| `spark.sql("SELECT" + " 1")` | `pyspark` | yes, folded |
| `self.spark.sql("""...""")` | `pyspark` | yes |
| `spark.sql(f"SELECT {table}")` | — | no, an f-string is not knowable before it runs |
| `spark.sql(query)` | — | no, likewise |
| `conn.sql("SELECT 1")` | — | no, `conn` is not a Spark session |

The receiver is checked rather than every `.sql(...)` call in the file being matched.
Plenty of libraries expose a method by that name: database connections, query builders,
duckdb etc. and linting their arguments as Spark SQL produces violations against code this
tool has no business judging. `spark`, `spark_session`, `sparkSession`, `_spark` and
`_spark_session` are recognised by default, on a name or as the last component of an
attribute chain, so `self.spark` and `ctx.spark` both count. `--sql-receiver NAME` adds
one, and `--sql-receiver '*'` restores matching everything.

### Where violations are reported

Never against the scratch file sqlfluff actually read. Where the source can say exactly
where the SQL sits in the host file, the violation is an ordinary clickable position; where
it cannot, it is the position within the snippet, and the header carries the path to the
SQL instead:

```text
== [jobs/pipeline.py: spark.sql] FAIL
jobs/pipeline.py:42:9 |  L010 | Keywords must be upper case.

== [dashboards/runs.lvdash.json: datasets[broken].queryLines] FAIL
L:   1 | P:  13 |   PRS | Couldn't find closing bracket for opening bracket.
```

### Queries and expressions are checked differently

A dataset query and a `spark.sql` string are written by a human and are linted with the
full configured rule set.

A field expression is a fragment such as ``COUNT(`update_id`)`` that the Databricks UI
generates and rewrites whenever someone edits a visualisation. Style rules have nothing
useful to say about it, so expressions are checked with every rule switched off and only
the parser running — unparsable input is still reported as a `PRS` violation, which is the
part that matters. Each expression is wrapped in `SELECT <expression>` to make it a
statement; the width of that wrapper is subtracted again from any column reported on the
expression's first line, so it is invisible in the output.

## Fixing

`--fix` runs `sqlfluff fix` over the extracted SQL and writes the result back into the file
it came from, then lints again and reports whatever sqlfluff could not fix.

```shell
databricks-validator --fix jobs/pipeline.py
```

For python that means the string literal is rebuilt in the style of the one it replaces:
the original quoting, prefix and indentation are kept, and SQL that grew a line break
during the fix is promoted to a triple-quoted literal laid out at the call's indentation.
Literals that cannot be rebuilt safely — an f-string, a `b`-prefixed literal — are reported
and left alone, as is any snippet carrying an inline `-- sqlfluff:disable` or `-- noqa`
directive.

**Dashboards are lint-only for now.** `--fix` on a `*.lvdash.json` is a usage error rather
than a no-op.

## Usage

### As a pre-commit hook

The `databricks-validator` hook covers both formats:

```yaml
repos:
- repo: https://github.com/godatadriven/databricks-validator
  rev: v3.0.1
  hooks:
  - id: databricks-validator
```

It runs on `*.lvdash.json` and `*.py`. To check only one of them, use the narrower ids:

| Hook id | Runs on | Needs docker |
| --- | --- | --- |
| `databricks-validator` | `*.lvdash.json`, `*.py` | no |
| `databricks-validator-fix` | `*.lvdash.json`, `*.py` | no |
| `validate-dashboard-sql-python` | `*.lvdash.json` | no |
| `sqlfluff-pyspark-lint` | `*.py` | no |
| `sqlfluff-pyspark-fix` | `*.py` | no |
| `validate-dashboard-sql` | `*.lvdash.json` | yes, built here |
| `validate-dashboard-sql-docker-latest` | `*.lvdash.json` | yes, `:latest` |
| `validate-dashboard-sql-docker-release` | `*.lvdash.json` | yes, `:v3.0.1` |

The docker ids build or pull an image instead of building a virtualenv; they exist for
repositories that would rather pull an image than have pre-commit install anything. They do
the same work and print the same thing.

The fix hooks modify files, so they are usually run by hand rather than on every commit:

```shell
pre-commit run databricks-validator-fix --all-files
```

### Outside pre-commit

An ordinary python package, so `uvx` runs it without installing anything permanently:

```shell
uvx --from git+https://github.com/godatadriven/databricks-validator@v3.0.1 \
  databricks-validator dashboards/*.lvdash.json jobs/*.py
```

Installed into an environment of its own:

```shell
pip install git+https://github.com/godatadriven/databricks-validator@v3.0.1
databricks-validator dashboards/*.lvdash.json
```

Or against the pre-built image:

```shell
docker run --rm --volume "${PWD}:/src:ro" --workdir /src \
  ghcr.io/godatadriven/databricks-validator:latest \
  dashboards/pipeline_runs.lvdash.json
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Nothing to report |
| 1 | Violations found, or a file could not be read |
| 2 | The validator was invoked wrongly |

## Configuration

| Flag | Default | Meaning |
| --- | --- | --- |
| `--fix` | off | Apply sqlfluff's fixes, then report what is left |
| `--dialect DIALECT` | `databricks` | sqlfluff dialect, for every kind |
| `--query-mode lint\|off` | `lint` | How to check dataset queries |
| `--expression-mode lint\|off` | `lint` | How to check widget expressions |
| `--pyspark-mode lint\|off` | `lint` | How to check `spark.sql()` strings |
| `--config FILE` | see below | sqlfluff config for dataset queries |
| `--expression-config FILE` | bundled | sqlfluff config for expressions |
| `--pyspark-config FILE` | see below | sqlfluff config for `spark.sql()` strings |
| `--sql-receiver NAME` (repeatable) | see above | Treat `NAME.sql(...)` as Spark SQL |
| `--sqlfluff-arg ARG` (repeatable) | empty | Extra flag passed straight to sqlfluff |
| `--keep-tmp` | off | Keep the extracted `.sql` files for inspection |
| `--verbose`, `-v` | off | Also report which snippet came from where |

Pass options through `args:`.

```yaml
- repo: https://github.com/godatadriven/databricks-validator
  rev: v3.0.1
  hooks:
  - id: databricks-validator
    args: [--dialect, sparksql, --expression-mode, 'off']
```

`--sqlfluff-arg` will not accept `--format`, `--dialect` or `--ignore-local-config`: the
validator sets all three itself, and reads the violations back out of sqlfluff's json
output, so overriding them would break the report rather than configure it.

### What the hook prints

By default the output is the sqlfluff run and nothing else: how many snippets of each kind
were checked, with which config, and every violation against the place it came from.

`--verbose` adds an inventory of every snippet found and the file it came from. That is
debugging output — a violation already names its own origin, so the inventory mostly gets
in the way of reading the failure:

```text
Extracting SQL from pipeline_runs.lvdash.json
  Found #1 query      datasets[pipeline_runs].queryLines
  Found #2 expression pages[runs].layout[total].widget.queries[main_query].query.fields[count(update_id)].expression
```

### Choosing the rule set

sqlfluff resolves config relative to the file it is linting, and the extracted snippets
live in a scratch directory outside your repository, so the config has to be passed
explicitly. For dataset queries and `spark.sql` strings the order is:

1. `--config FILE` (or `--pyspark-config FILE`), if given.
2. `.sqlfluff` in the working directory — the root of your repository, under pre-commit.
3. `pyproject.toml` in the working directory, if it has a `[tool.sqlfluff...]` section.
   A `pyproject.toml` that says nothing about sqlfluff is ignored, so an unrelated one does
   not quietly displace the bundled defaults or shadow a `.sqlfluff` sitting beside it.
4. The bundled default.

Whichever one wins is the only config in play: the validator passes
`--ignore-local-config` so sqlfluff does not search its default locations on top of it.
That keeps the config named in the output honest, and keeps a stray `~/.sqlfluff` from
changing results between a laptop and CI.

Passing the config explicitly matters for more than rule selection. Left to itself sqlfluff
reads the working directory's config for core settings but resolves the nested sections
against the snippet's own path — out in the scratch directory, where there is no config at
all. A project that configures a templater keeps the templater and loses the settings that
parameterise it:

```toml
[tool.sqlfluff.core]
templater = "placeholder"

[tool.sqlfluff.templater.placeholder]
param_regex = '...'
```

which fails before linting anything, with
`ValueError: No param_regex nor param_style was provided to the placeholder templater!`.
Naming the file on the command line keeps the two halves together.

Widget expressions are the exception. They are fragments rather than statements, so they
are always checked with the bundled syntax-only config regardless of what your repository
configures — including its templater.

One thing to watch for when your repository's config takes over: a config written for dbt
models or hand written SQL may switch off the very check this hook is for. `ignore =
"parsing"` in particular downgrades `PRS` to nothing, so a query with an unbalanced bracket
passes and the hook exits 0. If your repository config sets it, point the hook at a config
of its own instead:

```yaml
  - id: databricks-validator
    args: [--config, dashboards/.sqlfluff]
```

### The bundled defaults

Three configs ship inside the package, one per kind.

`sqlfluff-defaults.cfg` (dataset queries) is deliberately syntax first rather than style
first. Dashboard SQL is edited through the Databricks UI as often as it is edited by hand,
so a hook that fails a commit over indentation is a hook that gets disabled within a week.
It switches off:

- `layout` — whitespace and indentation, which the Databricks editor owns anyway.
- `references.from` — false positives on struct access such as
  `putl.trigger_details.job_task.job_id`.
- `references.qualification` — unqualified column names are idiomatic in dashboard queries.
- `structure.column_order` — pure style.

Running the full rule set over the example dashboard produces about ninety violations,
nearly all of them indentation. To opt back in, drop a `.sqlfluff` in your repository root:

```ini
[sqlfluff]
dialect = databricks
```

`sqlfluff-expressions.cfg` (widget expressions) switches every rule off and runs only the
parser.

`sqlfluff-pyspark.cfg` (`spark.sql` strings) keeps the layout rules on. SQL written inside
a python file is maintained by hand and reformatting it is the point of `--fix`, so there
is no editor upstream whose formatting has to be tolerated.

## Example dashboards

`examples/` holds three real dashboards twice over: `examples/raw/` as the Databricks UI
exported them, and `examples/cleaned/` with every sqlfluff violation fixed. Only the
cleaned copies are checked by this repository's own pre-commit hook.

[examples/README.md](examples/README.md) records what changed between the two and why, rule
by rule — a worked example of what this hook asks of a dashboard, and a starting point if
you are about to clean up your own.

## Migrating

Both commands and all the hook ids from the two tools this merges still work. Point `repo:`
at this repository, keep the `id:` you had, and update `rev:`.

Four things behave differently.

**`sqlfluff-pyspark` now defaults to the `databricks` dialect.** It previously passed no
`--dialect` at all, so unless your repository's config named one, Spark SQL was being
parsed as `ansi`. Expect violations that were previously invisible. Pass `--dialect ansi`
to keep the old behaviour, or `--dialect sparksql` for open-source Spark.

**`sqlfluff-pyspark` no longer matches every `.sql(...)` call.** It matched any attribute
call named `sql`, including `conn.sql(...)` and `duckdb.sql(...)`. Only Spark session
receivers are picked up now — see [From python](#from-python). This is a narrowing, so some
calls stop being checked; `--sql-receiver NAME` adds a receiver back, and
`--sql-receiver '*'` restores the old behaviour exactly.

**`sqlfluff-pyspark` reads its config the way the dashboard tool always did.** It used to
take any `pyproject.toml` that existed as its sqlfluff config, which meant an unrelated one
shadowed a real `.sqlfluff` beside it. A `pyproject.toml` now has to have a
`[tool.sqlfluff]` section to count. It also runs the sqlfluff that is installed alongside
it rather than whatever is on `PATH`, and passes `--ignore-local-config`.

**`--sqlfluff-arg --format json` is now a usage error.** Violations are read back out of
sqlfluff's json output, so `--format` is not available to pass through. Other
`--sqlfluff-arg` values are unaffected.

## Development

```shell
uv sync --group dev
uv run pytest
```

The behavioural tests drive the commands as subprocesses rather than calling `main()`
in-process, because the exit status and the exact bytes on stdout and stderr are the
contract a pre-commit hook is judged by, and only a real process proves them.

`uv run pre-commit run --all-files` runs the repository's own hooks, which include this
tool checked against the cleaned example dashboards.

### Adding a source

A source is a module under `src/databricks_validator/sources/` that turns one host format
into `Snippet`s. It needs `matches(path)`, `extract(path, start)`, and — if it can write
fixes back — a `rewrite` on each snippet and a `flush` on the `Extraction`. Everything
downstream of that is shared: scratch files, invoking sqlfluff, mapping violations back,
applying fixes. See `sources/base.py` for the contract and `core/snippet.py` for what a
source may fill in.

## License

MIT. See [LICENSE](LICENSE)
