import csv
import json
import sys
from pathlib import Path

import pytest

from scripts import detector_report, generate_proposal
from scripts.smell_identity import generate_smell_uuid


@pytest.mark.parametrize(
    "model_context",
    [None, {"File": "model-selected.py", "Related Participants": "[]"}],
)
def test_main_creates_expected_artifacts(tmp_path, report_dir, monkeypatch, model_context):
    # Build a filtered CSV with UUID column pointing to absolute fixture paths
    with (report_dir / "detector_report.csv").open() as fp:
        reader = csv.DictReader(fp)
        rows = list(reader)

    first_row = rows[0]
    first_row["File"] = str(Path(first_row["File"]).resolve())
    participants = json.loads(first_row["Related Participants"])
    for participant in participants:
        participant["file_path"] = str(Path(participant["file_path"]).resolve())
    first_row["Related Participants"] = json.dumps(participants)
    first_row["UUID"] = generate_smell_uuid(first_row)

    filtered_report = tmp_path / "filtered_report.csv"
    with filtered_report.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=first_row.keys())
        writer.writeheader()
        writer.writerow(first_row)

    fake_plan = {"strategy": "Interface", "explanation": "demo", "actions": [{"type": "noop"}]}
    if model_context is not None:
        fake_plan["similarity_context"] = model_context

    monkeypatch.setattr(sys, "argv", [
        "generate_proposal.py",
        "--smell-uuid",
        first_row["UUID"],
        "--report-file",
        str(filtered_report),
    ])
    monkeypatch.setattr(generate_proposal, "generate_refactor_plan", lambda *args, **kwargs: fake_plan)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        generate_proposal.main()

    assert exc.value.code == 0

    modules_file = tmp_path / "modules.json"
    prompt_file = tmp_path / "prompt.txt"
    proposal_file = tmp_path / "proposal.json"

    assert modules_file.exists()
    assert prompt_file.exists()
    assert proposal_file.exists()

    loaded_modules = json.loads(modules_file.read_text())
    assert list(loaded_modules)  # at least one module captured

    loaded_proposal = json.loads(proposal_file.read_text())
    # Check core fields match fake_plan
    assert loaded_proposal["strategy"] == fake_plan["strategy"]
    assert loaded_proposal["explanation"] == fake_plan["explanation"]
    assert loaded_proposal["actions"] == fake_plan["actions"]
    # importers_by_participant should exist on generated proposals
    assert "importers_by_participant" in loaded_proposal
    assert loaded_proposal["similarity_context"] == {
        "File": first_row["File"],
        "Related Participants": first_row["Related Participants"],
    }


@pytest.mark.parametrize("column", detector_report.METADATA_COLUMNS)
def test_main_rejects_malformed_report_before_planning(
    tmp_path, report_dir, monkeypatch, caplog, column
):
    with (report_dir / "detector_report.csv").open() as source:
        row = next(csv.DictReader(source))
    smell_uuid = generate_smell_uuid(row)
    row[column] = "null"
    report = tmp_path / "filtered_report.csv"
    with report.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)

    monkeypatch.setattr(sys, "argv", [
        "generate_proposal.py",
        "--smell-uuid", smell_uuid,
        "--report-file", str(report),
    ])
    monkeypatch.setattr(
        generate_proposal, "generate_refactor_plan",
        lambda *args, **kwargs: pytest.fail("invalid report reached planning"),
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        generate_proposal.main()

    assert exc.value.code == 1
    assert column in caplog.text
    for name in ("modules.json", "prompt.txt", "proposal.json"):
        assert not (tmp_path / name).exists()


def test_main_exits_when_modules_missing(tmp_path, monkeypatch):
    row = {
        "Name": "Cyclic Dependency",
        "File": str(tmp_path / "missing.py"),
        "Line Number": "1",
        "Description": "Missing file",
        "Severity": "Critical",
        "Snippet": "",
        "Related Participants": "[]",
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
    }
    row["UUID"] = generate_smell_uuid(row)

    filtered_report = tmp_path / "filtered_report.csv"
    with filtered_report.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)

    monkeypatch.setattr(sys, "argv", [
        "generate_proposal.py",
        "--smell-uuid",
        row["UUID"],
        "--report-file",
        str(filtered_report),
    ])
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        generate_proposal.main()

    assert exc.value.code == 1


def test_main_exits_when_plan_has_no_actions(tmp_path, report_dir, monkeypatch):
    with (report_dir / "detector_report.csv").open() as fp:
        reader = csv.DictReader(fp)
        row = next(reader)

    row["File"] = str(Path(row["File"]).resolve())
    row["Related Participants"] = json.dumps([
        {
            "file_path": str(Path(row["File"]).resolve()),
            "element_name": "cyclic_pkg.a",
            "element_type": "module",
        }
    ])
    row["UUID"] = generate_smell_uuid(row)

    filtered_report = tmp_path / "filtered_report.csv"
    with filtered_report.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)

    monkeypatch.setattr(sys, "argv", [
        "generate_proposal.py",
        "--smell-uuid",
        row["UUID"],
        "--report-file",
        str(filtered_report),
    ])
    monkeypatch.setattr(generate_proposal, "generate_refactor_plan", lambda *args, **kwargs: {"actions": []})
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        generate_proposal.main()

    assert exc.value.code == 1
