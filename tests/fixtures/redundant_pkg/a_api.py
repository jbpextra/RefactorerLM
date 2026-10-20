"""Thin wrapper exposing the same behaviour as base.do_thing."""

from . import base


def run() -> str:
    return base.do_thing()
