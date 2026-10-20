"""Second half of the cyclic import used by tests."""

from . import a


def fb() -> int:
    return a.fa() - 1
