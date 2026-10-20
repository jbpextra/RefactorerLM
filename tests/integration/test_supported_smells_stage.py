"""Supported refactoring types remain mandatory across pipeline stages."""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import filter_smells, generate_proposal
from scripts.supported_smells import get_smell_type


@pytest.fixture
def detector_rows(report_dir):
    with (report_dir / "detector_report.csv").open() as source:
        return list(csv.DictReader(source))


def _run_selection(tmp_path, monkeypatch, rows, config, existing_branches=""):
    report = tmp_path / "report.csv"
    config_path = tmp_path / "config.yml"
    output = tmp_path / "smells_to_process.json"
    with report.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    config_path.write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "filter_smells.py",
            "--report-file",
            str(report),
            "--config",
            str(config_path),
            "--output-file",
            str(output),
            "--existing-branches",
            existing_branches,
        ],
    )
    with pytest.raises(SystemExit) as result:
        filter_smells.main()
    assert result.value.code == 0
    with (tmp_path / "filtered_report.csv").open() as source:
        reader = csv.DictReader(source)
        selected_rows = list(reader)
        assert reader.fieldnames == list(rows[0]) + ["UUID"]
    return selected_rows, json.loads(output.read_text())


@pytest.mark.parametrize("filter_enabled", [False, True])
@pytest.mark.parametrize("limit", [0, 1, 2])
def test_selection_excludes_unsupported_before_repository_rules_and_limit(
    tmp_path, monkeypatch, detector_rows, filter_enabled, limit
):
    unsupported = {
        **detector_rows[0],
        "Name": "Ambiguous Interface",
    }
    config = {
        "smell_filter": {
            "filter_enabled": filter_enabled,
            "include_names": [],
            "include_severities": [],
        },
        "max_proposal_limit": limit,
    }
    selected, uuids = _run_selection(
        tmp_path, monkeypatch, [unsupported, *detector_rows], config
    )

    expected_rows = detector_rows[:limit] if limit else detector_rows
    assert sorted(row["Name"] for row in selected) == sorted(
        row["Name"] for row in expected_rows
    )
    assert set(uuids) == {filter_smells.generate_smell_uuid(row) for row in expected_rows}
    assert uuids == [row["UUID"] for row in selected]
    assert (tmp_path / "smells_detected.txt").read_text() == "true"


@pytest.mark.parametrize("filter_enabled", [False, True])
def test_unsupported_only_report_produces_empty_selection(
    tmp_path, monkeypatch, detector_rows, filter_enabled
):
    unsupported = {**detector_rows[0], "Name": "Hub-like Dependency"}
    selected, uuids = _run_selection(
        tmp_path,
        monkeypatch,
        [unsupported],
        {"smell_filter": {"filter_enabled": filter_enabled, "include_names": []}},
    )

    assert selected == []
    assert uuids == []
    # This flag is the workflow job condition; false prevents attempt jobs.
    assert (tmp_path / "smells_detected.txt").read_text() == "false"


def test_previous_attempts_do_not_consume_supported_selection_slots(
    tmp_path, monkeypatch, detector_rows
):
    previous_uuid = filter_smells.generate_smell_uuid(detector_rows[0])
    unsupported = {**detector_rows[0], "Name": "Ambiguous Interface"}
    selected, uuids = _run_selection(
        tmp_path,
        monkeypatch,
        [unsupported, *detector_rows],
        {"max_proposal_limit": 1, "smell_filter": {"filter_enabled": False}},
        existing_branches=f"origin/refactor/{previous_uuid}",
    )

    assert [row["Name"] for row in selected] == ["Potential Redundant Abstractions"]
    assert uuids == [filter_smells.generate_smell_uuid(detector_rows[1])]


def test_case_variants_share_cycle_grouping_and_preserve_detector_names(
    tmp_path, monkeypatch, detector_rows
):
    cycle = {
        **detector_rows[0],
        "Name": "cYcLiC dEpEnDeNcY",
        "File": "tests/fixtures/cyclic_pkg/b.py",
        "Severity": "High",
    }
    redundant = {**detector_rows[1], "Name": "POTENTIAL REDUNDANT ABSTRACTIONS"}
    selected, _ = _run_selection(
        tmp_path,
        monkeypatch,
        [detector_rows[0], cycle, redundant],
        {"smell_filter": {"filter_enabled": False}},
    )

    assert sorted(row["Name"] for row in selected) == sorted(
        [cycle["Name"], redundant["Name"]]
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Cyclic Dependency", "cyclic_dependency"),
        ("cyclic dependency", "cyclic_dependency"),
        ("CYCLIC DEPENDENCY", "cyclic_dependency"),
        ("Potential Redundant Abstractions", "unneeded_abstraction"),
        ("potential redundant abstractions", "unneeded_abstraction"),
        ("POTENTIAL REDUNDANT ABSTRACTIONS", "unneeded_abstraction"),
    ],
)
def test_selection_and_generation_agree_on_supported_types(name, expected):
    row = {"Name": name}
    assert get_smell_type(name) == expected
    assert filter_smells.filter_supported_smells([{"row": row}]) == [{"row": row}]
    assert generate_proposal.map_smell_to_type(name) == expected


@pytest.mark.parametrize(
    "name", ["Hub-like Dependency", "Redundant Abstractions", "Unneeded Abstraction", ""]
)
def test_unsupported_names_do_not_gain_refactoring_capabilities(name):
    assert get_smell_type(name) is None
    assert filter_smells.filter_supported_smells([{"row": {"Name": name}}]) == []
    with pytest.raises(NotImplementedError):
        generate_proposal.map_smell_to_type(name)


def test_module_selection_uses_tool_package_and_writes_in_target(tmp_path, detector_rows):
    report = tmp_path / "report.csv"
    with report.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(detector_rows[0]))
        writer.writeheader()
        writer.writerow({**detector_rows[0], "Name": "Hub-like Dependency"})
    config = tmp_path / "config.yml"
    config.write_text("smell_filter:\n  filter_enabled: false\n")
    caller_scripts = tmp_path / "scripts"
    caller_scripts.mkdir()
    (caller_scripts / "__init__.py").write_text(
        "raise RuntimeError('The caller scripts package must not be imported')\n"
    )
    tool_root = Path(filter_smells.__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "scripts.filter_smells",
            "--report-file",
            report.name,
            "--config",
            config.name,
            "--output-file",
            "smells_to_process.json",
            "--existing-branches",
            "",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tool_root)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "smells_to_process.json").read_text()) == []
    assert (tmp_path / "smells_detected.txt").read_text() == "false"
    with (tmp_path / "filtered_report.csv").open() as source:
        reader = csv.DictReader(source)
        assert reader.fieldnames == list(detector_rows[0]) + ["UUID"]
        assert list(reader) == []
