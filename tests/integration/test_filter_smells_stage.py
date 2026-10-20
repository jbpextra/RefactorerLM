"""The selection CLI writes the artifacts consumed by later pipeline stages."""

import csv
import json
import shutil
import sys

import pytest

from scripts import detector_report, filter_smells


def test_main_writes_filtered_artifacts(tmp_path, config_dir, report_dir, monkeypatch):
    report = tmp_path / "report.csv"
    config = tmp_path / "config.yml"
    output = tmp_path / "uuids.json"

    shutil.copy(report_dir / "detector_report.csv", report)
    shutil.copy(config_dir / "basic.yml", config)

    argv = [
        "filter_smells.py",
        "--report-file",
        str(report),
        "--config",
        str(config),
        "--output-file",
        str(output),
        "--existing-branches",
        "",
    ]

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        filter_smells.main()

    assert exc.value.code == 0

    filtered_report = tmp_path / "filtered_report.csv"
    smells_detected = tmp_path / "smells_detected.txt"

    assert output.exists()
    assert filtered_report.exists()
    assert smells_detected.exists()

    uuids = json.loads(output.read_text())
    assert uuids, "Expected UUIDs to be written"
    assert smells_detected.read_text() == "true"


def test_main_writes_false_when_no_smells(tmp_path, config_dir, monkeypatch):
    report = tmp_path / "report.csv"
    config = tmp_path / "config.yml"
    output = tmp_path / "uuids.json"

    with report.open("w", newline="") as fp:
        writer = csv.DictWriter(
            fp, fieldnames=["Name", "Severity", "File", *detector_report.METADATA_COLUMNS]
        )
        writer.writeheader()

    shutil.copy(config_dir / "basic.yml", config)

    argv = [
        "filter_smells.py",
        "--report-file",
        str(report),
        "--config",
        str(config),
        "--output-file",
        str(output),
        "--existing-branches",
        "",
    ]

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        filter_smells.main()

    assert exc.value.code == 0

    smells_detected = tmp_path / "smells_detected.txt"
    assert smells_detected.exists()
    assert smells_detected.read_text() == "false"
    assert output.exists()
    assert json.loads(output.read_text()) == []


def test_main_handles_large_csv_fields(tmp_path, config_dir, monkeypatch):
    report = tmp_path / "report.csv"
    config = tmp_path / "config.yml"
    output = tmp_path / "uuids.json"

    with report.open("w", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "Name",
                "File",
                "Line Number",
                "Description",
                "Severity",
                "Related Participants",
                "Importers By Participant",
                "Participant Dependency Edges",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Name": "Cyclic Dependency",
                "File": "pkg/a.py",
                "Line Number": "1",
                "Description": "x" * 200000,
                "Severity": "Critical",
                "Importers By Participant": "{}",
                "Participant Dependency Edges": "[]",
                "Related Participants": json.dumps(
                    [
                        {"element_name": "A", "element_type": "module", "file_path": "pkg/a.py"},
                        {"element_name": "B", "element_type": "module", "file_path": "pkg/b.py"},
                    ]
                ),
            }
        )

    shutil.copy(config_dir / "basic.yml", config)

    argv = [
        "filter_smells.py",
        "--report-file",
        str(report),
        "--config",
        str(config),
        "--output-file",
        str(output),
        "--existing-branches",
        "",
    ]

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        filter_smells.main()

    assert exc.value.code == 0
    assert json.loads(output.read_text())
