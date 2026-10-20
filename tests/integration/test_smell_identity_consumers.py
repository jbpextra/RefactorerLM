"""All report consumers join on the canonical identity, not a stale CSV UUID."""

import csv
import json

import pytest

from scripts import filter_smells, generate_pr_description, generate_proposal
from scripts.generate_summary import generate_summary


@pytest.fixture
def smell_row():
    return {
        "Name": "Cyclic Dependency",
        "File": "pkg/A.py",
        "Description": "Original detector wording",
        "Line Number": "10",
        "Severity": "medium",
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
        "Related Participants": json.dumps(
            [
                {
                    "element_name": "pkg.A",
                    "element_type": "module",
                    "file_path": "pkg/A.py",
                    "role": "Member of cycle",
                },
                {
                    "element_name": "pkg.B",
                    "element_type": "module",
                    "file_path": "pkg/B.py",
                    "role": "Member of cycle",
                },
            ]
        ),
        "UUID": "stale-stored-identifier",
    }


def write_report(path, row):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)


def changed_metadata(row):
    updated = dict(row)
    updated.update(
        {
            "Description": "Reworded detector explanation",
            "Line Number": "200",
            "Severity": "high",
            "File": "./pkg/sub/../A.py",
        }
    )
    participants = json.loads(row["Related Participants"])
    for participant in participants:
        participant["role"] = "Changed reporting role"
        participant["file_path"] = "./" + participant["file_path"]
    updated["Related Participants"] = json.dumps(list(reversed(participants)))
    return updated


@pytest.mark.parametrize("update_metadata", [False, True])
def test_lookups_use_canonical_identity(tmp_path, smell_row, update_metadata):
    smell_uuid = filter_smells.generate_smell_uuid(smell_row)
    report_row = changed_metadata(smell_row) if update_metadata else smell_row
    report = tmp_path / "filtered_report.csv"
    write_report(report, report_row)

    proposal_row = generate_proposal.extract_smell_details(smell_uuid, str(report))
    description_row = generate_pr_description.find_smell_in_report(report, smell_uuid)

    assert proposal_row is not None
    assert proposal_row["row"] == description_row
    assert proposal_row["row"]["UUID"] == smell_uuid
    assert proposal_row["row"]["Description"] == report_row["Description"]


def test_lookups_reject_stale_stored_uuid(tmp_path, smell_row):
    report = tmp_path / "filtered_report.csv"
    write_report(report, smell_row)

    assert (
        generate_proposal.extract_smell_details(smell_row["UUID"], str(report)) is None
    )
    with pytest.raises(ValueError, match="not found"):
        generate_pr_description.find_smell_in_report(report, smell_row["UUID"])


def test_summary_joins_selection_after_metadata_changes(tmp_path, smell_row):
    smell_uuid = filter_smells.generate_smell_uuid(smell_row)
    report = tmp_path / "report.csv"
    filtered_report = tmp_path / "filtered_report.csv"
    selected = tmp_path / "smells_to_process.json"
    write_report(report, smell_row)
    write_report(filtered_report, changed_metadata(smell_row))
    selected.write_text(json.dumps([smell_uuid]), encoding="utf-8")

    summary = generate_summary(report, filtered_report, selected)

    assert "**1** of 1" in summary
    assert f"| high | `{smell_uuid[:8]}...` |" in summary
    assert smell_row["UUID"] not in summary
