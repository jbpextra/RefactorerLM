"""Minimal module participating in a cycle with b."""

from . import b


def fa() -> int:
    return 1 + b.fb()
