"""Selection must establish all input contracts before producing attempt artifacts."""

import csv
import json
import sys

import pytest

from scripts import detector_report, filter_smells


def _run_selection(
    tmp_path, report_dir, monkeypatch, *, changes=None, missing=None, config="{}"
):
    with (report_dir / "detector_report.csv").open() as source:
        row = next(csv.DictReader(source))
    row.update(changes or {})
    if missing:
        del row[missing]
    report = tmp_path / "report.csv"
    with report.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)
    (tmp_path / "config.yml").write_text(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "filter_smells.py",
            "--report-file", str(report),
            "--config", str(tmp_path / "config.yml"),
            "--output-file", str(tmp_path / "uuids.json"),
            "--existing-branches", "",
        ],
    )
    with pytest.raises(SystemExit) as result:
        filter_smells.main()
    return result.value.code


def _assert_no_attempt_artifacts(tmp_path):
    for name in ("uuids.json", "filtered_report.csv", "smells_detected.txt"):
        assert not (tmp_path / name).exists()


@pytest.mark.parametrize("column", detector_report.METADATA_COLUMNS)
def test_missing_metadata_column_stops_selection(
    tmp_path, report_dir, monkeypatch, caplog, column
):
    assert _run_selection(tmp_path, report_dir, monkeypatch, missing=column) == 1
    assert column in caplog.text
    _assert_no_attempt_artifacts(tmp_path)


@pytest.mark.parametrize("column", detector_report.METADATA_COLUMNS)
@pytest.mark.parametrize("raw", ["", "null", "not-json", '"wrong container"'])
def test_invalid_metadata_stops_even_when_config_excludes_row(
    tmp_path, report_dir, monkeypatch, caplog, column, raw
):
    config = "smell_filter:\n  filter_enabled: true\n  include_names: [Excluded]\n"
    assert _run_selection(
        tmp_path, report_dir, monkeypatch, changes={column: raw}, config=config
    ) == 1
    assert column in caplog.text
    assert "line 2" in caplog.text
    _assert_no_attempt_artifacts(tmp_path)


@pytest.mark.parametrize("column,data", [
    ("Related Participants", ["a.py"]),
    ("Related Participants", [{"element_name": "a", "element_type": "module"}]),
    ("Related Participants", [{"file_path": "a.py", "element_type": "module"}]),
    ("Related Participants", [{"file_path": "a.py", "element_name": "a"}]),
    ("Related Participants", [{"file_path": 7, "element_name": "a", "element_type": "module"}]),
    ("Importers By Participant", {"a.py": None}),
    ("Importers By Participant", {"a.py": "b.py"}),
    ("Importers By Participant", {"a.py": [None]}),
    ("Importers By Participant", {"a.py": [42]}),
    ("Importers By Participant", {"a.py": [""]}),
    ("Importers By Participant", {"": ["b.py"]}),
    ("Participant Dependency Edges", [["a.py"]]),
    ("Participant Dependency Edges", [["a.py", "b.py", "c.py"]]),
    ("Participant Dependency Edges", ["a.py"]),
    ("Participant Dependency Edges", [["a.py", None]]),
    ("Participant Dependency Edges", [["", "b.py"]]),
])
def test_malformed_metadata_entries_stop_selection(
    tmp_path, report_dir, monkeypatch, caplog, column, data
):
    assert _run_selection(
        tmp_path, report_dir, monkeypatch, changes={column: json.dumps(data)}
    ) == 1
    assert column in caplog.text
    _assert_no_attempt_artifacts(tmp_path)


def test_duplicate_importer_key_cannot_hide_importers(tmp_path, report_dir, monkeypatch):
    assert _run_selection(
        tmp_path, report_dir, monkeypatch,
        changes={"Importers By Participant": '{"a.py": ["b.py"], "a.py": []}'},
    ) == 1
    _assert_no_attempt_artifacts(tmp_path)


@pytest.mark.parametrize("importers", ["{}", '{"a.py": []}'])
def test_explicit_empty_graph_metadata_is_valid(
    tmp_path, report_dir, monkeypatch, importers
):
    assert _run_selection(
        tmp_path, report_dir, monkeypatch,
        changes={
            "Importers By Participant": importers,
            "Participant Dependency Edges": "[]",
        },
    ) == 0
    assert json.loads((tmp_path / "uuids.json").read_text())


def test_empty_participants_remain_valid_for_generic_detector_rows(
    tmp_path, report_dir, monkeypatch
):
    assert _run_selection(
        tmp_path, report_dir, monkeypatch,
        changes={
            "Name": "Orphan Module",
            "Related Participants": "[]",
            "Importers By Participant": "{}",
            "Participant Dependency Edges": "[]",
        },
    ) == 0
    assert json.loads((tmp_path / "uuids.json").read_text()) == []


@pytest.mark.parametrize(
    "config", ["smell_filter: [", "smell_filter: null", "max_proposal_limit: -1"]
)
def test_invalid_config_stops_selection(tmp_path, report_dir, monkeypatch, caplog, config):
    assert _run_selection(tmp_path, report_dir, monkeypatch, config=config) == 1
    assert "config" in caplog.text.lower()
    _assert_no_attempt_artifacts(tmp_path)
