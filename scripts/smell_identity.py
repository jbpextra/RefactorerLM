"""Stable smell identities shared by selection and artifact consumers."""

import json
import posixpath
import uuid
from collections.abc import Mapping


def normalize_path(path: str) -> str:
    """Normalize detector paths lexically, preserving case and relativity."""
    return posixpath.normpath(path.replace("\\", "/")) if path else ""


def normalize_smell_type(name: str) -> str:
    """Smell labels are case insensitive; filesystem paths and identifiers are not."""
    return " ".join(name.split()).casefold()


def _text(data: Mapping, field: str) -> str:
    value = data.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"Identity field {field!r} must be a string")
    return value


def generate_smell_uuid(smell: Mapping, *, participants=None) -> str:
    """Identify a smell by normalized type, primary path and participant set.

    Paths and participant identifiers retain case. Other metadata is excluded.
    Callers can supply parsed participants to avoid decoding the JSON again.
    """
    if participants is None:
        raw = smell.get("Related Participants", "")
        try:
            participants = [] if raw is None or raw == "" else json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("'Related Participants' must be a JSON list") from exc
    if participants is None:
        participants = []
    if not isinstance(participants, list):
        raise ValueError("'Related Participants' must be a JSON list")

    entries = set()
    for participant in participants:
        if not isinstance(participant, dict):
            raise ValueError("Each related participant must be an object")
        entries.add(
            (
                _text(participant, "element_name"),
                _text(participant, "element_type"),
                normalize_path(_text(participant, "file_path")),
            )
        )

    fingerprint = {
        "smell_type": normalize_smell_type(_text(smell, "Name")),
        "primary_path": normalize_path(_text(smell, "File")),
        "participants": [
            {"element_name": name, "element_type": kind, "file_path": path}
            for name, kind, path in sorted(entries)
        ],
    }
    seed = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
