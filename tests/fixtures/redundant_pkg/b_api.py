"""Another redundant wrapper referencing base.do_thing."""

from . import base


def run() -> str:
    return base.do_thing()
