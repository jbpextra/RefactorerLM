"""Detector names supported by the existing refactoring planners."""

from typing import Optional

from .smell_identity import normalize_smell_type


# Keys are normalized detector names; values are the existing LLM prompt types.
SUPPORTED_SMELL_TYPES = {
    "cyclic dependency": "cyclic_dependency",
    "potential redundant abstractions": "unneeded_abstraction",
}


def get_smell_type(smell_name: Optional[str]) -> Optional[str]:
    """Resolve a detector name case-insensitively, or return None if unsupported."""
    return SUPPORTED_SMELL_TYPES.get(normalize_smell_type(smell_name or ""))
