"""Parse detector CSV input once while preserving its original artifact fields."""

import csv
import json
from typing import Dict, List, Set, Tuple

from .smell_identity import normalize_path


CSV_FIELD_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MiB
METADATA_COLUMNS = (
    "Related Participants",
    "Importers By Participant",
    "Participant Dependency Edges",
)


def configure_csv_field_limit() -> None:
    """Allow detector CSV fields up to 50 MiB."""
    csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)


def _metadata_error(row: Dict, column: str, detail: str) -> ValueError:
    smell_id = row.get("UUID") or row.get("File") or "unknown smell"
    return ValueError(f"Invalid '{column}' for {smell_id}: {detail}")


def _parse_metadata(row: Dict, column: str, expected_type: type):
    """Read required detector JSON; only explicit empty collections mean empty."""
    raw = row.get(column)
    if not isinstance(raw, str) or not raw.strip():
        raise _metadata_error(row, column, "missing required JSON value")

    def unique_object(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError(f"duplicate object key {key!r}")
            data[key] = value
        return data

    try:
        data = json.loads(raw, object_pairs_hook=unique_object)
    except ValueError as exc:
        raise _metadata_error(row, column, f"malformed JSON ({exc})") from exc
    if not isinstance(data, expected_type):
        raise _metadata_error(row, column, f"expected {expected_type.__name__}")
    return data


def _require_text(row: Dict, column: str, value, detail: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _metadata_error(row, column, f"{detail} must be a nonempty string")
    return value


def parse_participants(row: Dict) -> List[Dict]:
    """Validate participant identities before UUID generation or cycle grouping."""
    column = "Related Participants"
    participants = _parse_metadata(row, column, list)
    for index, participant in enumerate(participants):
        if not isinstance(participant, dict):
            raise _metadata_error(row, column, f"participant {index} must be an object")
        for key in ("element_name", "element_type", "file_path"):
            _require_text(row, column, participant.get(key), f"participant {index}.{key}")
        if "role" in participant:
            _require_text(row, column, participant["role"], f"participant {index}.role")
        for key in ("start_line", "end_line"):
            if key in participant and type(participant[key]) is not int:
                raise _metadata_error(
                    row, column, f"participant {index}.{key} must be an integer"
                )
    return participants


def parse_importers_by_participant(row: Dict) -> Dict[str, List[str]]:
    """Parse 'Importers By Participant' column into {participant: [importers]}."""
    column = "Importers By Participant"
    data = _parse_metadata(row, column, dict)
    result: Dict[str, Set[str]] = {}
    for participant, importers in data.items():
        _require_text(row, column, participant, "participant path")
        if not isinstance(importers, list):
            raise _metadata_error(
                row, column, f"{participant}: expected list of importer paths"
            )
        normalized_importers = {
            normalize_path(_require_text(row, column, importer, "importer path"))
            for importer in importers
        }
        result.setdefault(normalize_path(participant), set()).update(normalized_importers)
    return {
        participant: sorted(importers)
        for participant, importers in sorted(result.items())
    }


def parse_dependency_edges(row: Dict) -> List[Tuple[str, str]]:
    """Parse 'Participant Dependency Edges' column into [(src, dst), ...]."""
    column = "Participant Dependency Edges"
    data = _parse_metadata(row, column, list)
    edges = set()
    for index, edge in enumerate(data):
        if not isinstance(edge, list) or len(edge) != 2:
            raise _metadata_error(row, column, f"edge {index} must be a pair of paths")
        src, dst = (
            normalize_path(_require_text(row, column, path, f"edge {index} endpoint"))
            for path in edge
        )
        edges.add((src, dst))
    return sorted(edges)


def parse_smell(row: Dict) -> Dict:
    """Validate one detector row and retain parsed metadata for stage logic."""
    return {
        "row": row,
        "participants": parse_participants(row),
        "importers": parse_importers_by_participant(row),
        "dependency_edges": parse_dependency_edges(row),
    }


def load_report(report_path: str) -> Tuple[List[Dict], List[str]]:
    """Reject incomplete structural metadata before any selection or UUID work."""
    configure_csv_field_limit()
    with open(report_path, newline="") as f:
        reader = csv.DictReader(f, strict=True)
        columns = reader.fieldnames or []
        for column in METADATA_COLUMNS:
            if columns.count(column) != 1:
                raise ValueError(f"Report requires exactly one '{column}' column")
        smells = []
        for row in reader:
            try:
                if None in row:
                    raise ValueError("too many CSV fields")
                smells.append(parse_smell(row))
            except ValueError as exc:
                raise ValueError(
                    f"Report {report_path}, line {reader.line_num}: {exc}"
                ) from exc
    return smells, columns
