"""The one exception type the command line raises."""

from __future__ import annotations


class UsageError(Exception):
    """A problem with how the validator was invoked. Reported, then exit status 2."""
