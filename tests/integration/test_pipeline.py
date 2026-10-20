import csv
import json
import sys
from pathlib import Path

import pytest

from scripts import apply_refactoring, correctness_filter, filter_smells, generate_proposal


def _write_absolute_report(src_report: Path, dest_report: Path) -> dict:
    with src_report.open() as fp:
        reader = csv.DictReader(fp)
        row = next(reader)

    participants = json.loads(row["Related Participants"])
    for participant in participants:
        participant["file_path"] = str(Path(participant["file_path"]).resolve())
    row["Related Participants"] = json.dumps(participants)
    row["File"] = str(Path(row["File"]).resolve())
    row["UUID"] = ""

    with dest_report.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)

    return row


def test_end_to_end_smoke(tmp_path, config_dir, report_dir, monkeypatch):
    report = tmp_path / "report.csv"
    config = tmp_path / "config.yml"
    proposal_file = tmp_path / "proposal.json"
    modules_file = tmp_path / "modules.json"

    row = _write_absolute_report(report_dir / "detector_report.csv", report)
    config.write_text((config_dir / "basic.yml").read_text())

    # Stage 1: filter_smells
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
            str(tmp_path / "uuids.json"),
            "--existing-branches",
            "",
        ],
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        filter_smells.main()
    assert exc.value.code == 0

    uuids = json.loads((tmp_path / "uuids.json").read_text())
    assert len(uuids) == 1
    smell_uuid = uuids[0]

    # Stage 2: generate_proposal
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_proposal.py",
            "--smell-uuid",
            smell_uuid,
            "--report-file",
            str(tmp_path / "filtered_report.csv"),
        ],
    )

    fake_plan = {"strategy": "Interface", "explanation": "demo", "actions": [{"type": "noop"}]}
    monkeypatch.setattr(generate_proposal, "generate_refactor_plan", lambda *args, **kwargs: fake_plan)

    with pytest.raises(SystemExit) as exc:
        generate_proposal.main()
    assert exc.value.code == 0

    assert proposal_file.exists()
    assert modules_file.exists()

    # Stage 3: apply_refactoring
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setattr(
        apply_refactoring,
        "execute_refactor",
        lambda plan, modules, importers_map: [
            {
                "path": "refactored/result.py",
                "operation": "create",
                "content": "# refactored\n",
            }
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            smell_uuid,
            "--proposal-file",
            str(proposal_file),
            "--modules-file",
            str(modules_file),
        ],
    )

    apply_refactoring.main()
    created_file = tmp_path / "refactored" / "result.py"
    assert created_file.exists()

    # Stage 4: correctness filter
    monkeypatch.setattr(
        correctness_filter.subprocess,
        "run",
        lambda *args, **kwargs: type("res", (), {"returncode": 0, "stdout": b"", "stderr": b""})(),
    )
    test_result = correctness_filter.run_repository_tests()
    assert test_result["status"] == "passed"
    assert "pytest exit code 0" in test_result["reason"]
