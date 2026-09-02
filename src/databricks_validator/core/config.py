"""Working out which sqlfluff config a snippet is checked with.

sqlfluff resolves config relative to the file it is linting, and the extracted snippets
live in a scratch directory outside the repository being checked, so the config has to be
named on the command line. Passing it is not only about picking the right rules: left to
itself sqlfluff still reads the working directory's config for core settings but resolves
the nested sections against the snippet's own path, where there is no config at all. A
project that sets

    [tool.sqlfluff.core]
    templater = "placeholder"
    [tool.sqlfluff.templater.placeholder]
    param_regex = '...'

then keeps the templater and loses the parameters, and sqlfluff dies with "No param_regex
nor param_style was provided to the placeholder templater!" before linting anything.
Naming the file keeps the two halves together.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The bundled configs ship inside the package, so they are found the same way whether the
# validator runs from a checkout, from a wheel in a pre-commit venv, or from the image.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DEFAULT_QUERY_CONFIG = DATA_DIR / "sqlfluff-defaults.cfg"
DEFAULT_EXPRESSION_CONFIG = DATA_DIR / "sqlfluff-expressions.cfg"
DEFAULT_PYSPARK_CONFIG = DATA_DIR / "sqlfluff-pyspark.cfg"

# A pyproject.toml counts as sqlfluff configuration only when it actually configures
# sqlfluff, hence matching the section header rather than the file's existence. Nearly
# every python project has a pyproject.toml, and treating an unrelated one as the config
# would quietly drop the bundled defaults — or, worse, shadow a real .sqlfluff sitting
# beside it.
_SQLFLUFF_SECTION = re.compile(r"^[ \t]*\[tool\.sqlfluff", re.MULTILINE)


def resolve_config(configured: str | None, fallback: Path) -> str:
    """Which config to lint with.

    Order: the flag, then the repository's own config, then the bundled default. sqlfluff
    reads its settings from either a .sqlfluff or a [tool.sqlfluff.*] section in
    pyproject.toml, so both count as "the repository's own".
    """
    if configured:
        return configured

    working_directory = os.getcwd()

    dot_sqlfluff = os.path.join(working_directory, ".sqlfluff")
    if os.path.isfile(dot_sqlfluff):
        return dot_sqlfluff

    pyproject = os.path.join(working_directory, "pyproject.toml")
    if os.path.isfile(pyproject) and configures_sqlfluff(pyproject):
        return pyproject

    return str(fallback)


def configures_sqlfluff(pyproject: str) -> bool:
    """Whether a pyproject.toml has anything to say about sqlfluff."""
    try:
        with open(pyproject, encoding="utf-8", errors="replace") as handle:
            return _SQLFLUFF_SECTION.search(handle.read()) is not None
    except OSError:
        return False
