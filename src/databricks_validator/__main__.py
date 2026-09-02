"""`python -m databricks_validator`, so the validator runs without its console script."""

import sys

from databricks_validator.cli import main

if __name__ == "__main__":
    sys.exit(main())
