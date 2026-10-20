"""Selection artifacts and previous-attempt markers share one smell identity."""

import csv
import json
import sys

import pytest

from scripts import detector_report, filter_smells
from scripts.smell_identity import generate_smell_uuid


def _smell(**changes):
    row = {
        "Name": "Potential Redundant Abstractions",
        "File": "pkg/A.py",
        "Description": "A and B are redundant wrappers",
        "Line Number": "10",
        "Severity": "High",
        "Related Participants": json.dumps(
            [
                {"element_name": "A", "element_type": "class", "file_path": "pkg/A.py"},
                {"element_name": "B", "element_type": "class", "file_path": "pkg/B.py"},
            ]
        ),
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
    }
    row.update(changes)
    return row


def _select(tmp_path, monkeypatch, rows, marker="", limit=0):
    report = tmp_path / "report.csv"
    with report.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    config = tmp_path / "config.yml"
    config.write_text(f"max_proposal_limit: {limit}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "filter_smells.py",
            "--report-file",
            str(report),
            "--config",
            str(config),
            "--output-file",
            "smells_to_process.json",
            "--existing-branches",
            f"origin/refactor/{marker}" if marker else "",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        filter_smells.main()
    assert exc.value.code == 0
    ids = json.loads((tmp_path / "smells_to_process.json").read_text())
    with (tmp_path / "filtered_report.csv").open() as stream:
        selected = list(csv.DictReader(stream))
    assert [row["UUID"] for row in selected] == ids
    assert (tmp_path / "smells_detected.txt").read_text() == ("true" if ids else "false")
    return ids, selected


def test_stable_marker_survives_metadata_and_path_spelling_changes(tmp_path, monkeypatch):
    original = _smell()
    changed = _smell(
        Name=" potential  REDUNDANT abstractions ",
        File="./pkg/sub/../A.py",
        Description="New detector wording",
        Severity="Low",
        **{"Line Number": "999"},
    )
    participants = json.loads(changed["Related Participants"])
    participants.reverse()
    for participant in participants:
        participant["file_path"] = "./" + participant["file_path"].replace("/", "\\")
        participant["role"] = "changed"
    changed["Related Participants"] = json.dumps(participants)
    ids, _ = _select(tmp_path, monkeypatch, [changed], generate_smell_uuid(original))
    assert ids == []


def test_stable_marker_skips_every_duplicate_of_the_identity(tmp_path, monkeypatch):
    original = _smell(Severity="Low")
    duplicate = _smell(Description="Preferred high severity row")
    for rows in ([original, duplicate], [duplicate, original]):
        ids, _ = _select(tmp_path, monkeypatch, rows, generate_smell_uuid(original))
        assert ids == []


def test_matrix_and_report_deduplicate_identity_before_quota(tmp_path, monkeypatch):
    first = _smell()
    duplicate = _smell(Description="Incidental duplicate", Severity="Low")
    other = _smell(File="pkg/C.py")
    ids, selected = _select(tmp_path, monkeypatch, [first, duplicate, other], limit=2)
    assert set(ids) == {generate_smell_uuid(first), generate_smell_uuid(other)}
    assert len(ids) == len(set(ids)) == 2
    reversed_ids, reversed_rows = _select(
        tmp_path, monkeypatch, [other, duplicate, first], limit=2
    )
    assert reversed_ids == ids
    assert reversed_rows == selected


@pytest.mark.parametrize(
    "changes",
    [
        {"Name": "Cyclic Dependency"},
        {"File": "pkg/C.py"},
        {
            "Related Participants": json.dumps(
                [{"element_name": "C", "element_type": "class", "file_path": "pkg/C.py"}]
            )
        },
    ],
)
def test_marker_does_not_suppress_meaningfully_changed_smell(tmp_path, monkeypatch, changes):
    original = _smell()
    changed = _smell(**changes)
    ids, _ = _select(tmp_path, monkeypatch, [changed], generate_smell_uuid(original))
    assert ids == [generate_smell_uuid(changed)]
    assert ids != [generate_smell_uuid(original)]


def test_cycle_representative_identity_is_independent_of_report_order():
    first = _smell(Name="Cyclic Dependency")
    second = _smell(Name="Cyclic Dependency")
    participants = json.loads(second["Related Participants"])
    participants[0]["element_name"] = "AnotherClassInSameFile"
    second["Related Participants"] = json.dumps(participants)
    smells = [detector_report.parse_smell(row) for row in (first, second)]
    selected = filter_smells.deduplicate_cyclic_dependencies(smells)
    reversed_selection = filter_smells.deduplicate_cyclic_dependencies(
        list(reversed(smells))
    )
    assert generate_smell_uuid(selected[0]["row"]) == generate_smell_uuid(
        reversed_selection[0]["row"]
    )
