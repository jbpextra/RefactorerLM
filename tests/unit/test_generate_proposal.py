import csv
import json

import pytest

from scripts import detector_report, generate_proposal
from scripts.smell_identity import generate_smell_uuid


def test_extract_modules_loads_all_importers_when_under_limit(tmp_path, cyclic_fixture_dir):
    participant = str(cyclic_fixture_dir / "a.py")
    importer_a = tmp_path / "importer_a.py"
    importer_b = tmp_path / "importer_b.py"
    importer_a.write_text("from x import y\n")
    importer_b.write_text("from x import y\n")

    smell = {
        "File": participant,
        "Related Participants": json.dumps([]),
        "Importers By Participant": json.dumps(
            {participant: [str(importer_b), str(importer_a)]}
        ),
        "Participant Dependency Edges": "[]",
    }

    modules, importers_selected = generate_proposal.extract_modules_for_refactoring(
        detector_report.parse_smell(smell)
    )

    assert participant in modules
    assert str(importer_a) in modules
    assert str(importer_b) in modules
    assert importers_selected == {
        participant: sorted([str(importer_a), str(importer_b)]),
    }


def test_map_smell_to_type_maps_supported_smells():
    assert generate_proposal.map_smell_to_type("Cyclic Dependency") == "cyclic_dependency"
    assert (
        generate_proposal.map_smell_to_type("Potential Redundant Abstractions")
        == "unneeded_abstraction"
    )

    with pytest.raises(NotImplementedError):
        generate_proposal.map_smell_to_type("Unknown Smell")


def test_extract_smell_details_finds_uuid(tmp_path):
    row = {
        "Name": "Cyclic Dependency",
        "File": "tests/fixtures/cyclic_pkg/a.py",
        "Line Number": "1",
        "Description": "Cycle detected",
        "Severity": "Critical",
        "Related Participants": "[]",
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
    }
    row["UUID"] = generate_smell_uuid(row)

    report = tmp_path / "filtered_report.csv"
    with report.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)

    found = generate_proposal.extract_smell_details(row["UUID"], str(report))
    assert found and found["row"] == row
    assert found["participants"] == []
    assert found["importers"] == {}
    assert found["dependency_edges"] == []


def test_extract_modules_for_refactoring_reads_files(cyclic_fixture_dir):
    smell = {
        "File": str(cyclic_fixture_dir / "a.py"),
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
        "Related Participants": json.dumps(
            [
                {
                    "file_path": str(cyclic_fixture_dir / "b.py"),
                    "element_name": "cyclic_pkg.b",
                    "element_type": "module",
                }
            ]
        ),
    }

    modules, importers_selected = generate_proposal.extract_modules_for_refactoring(
        detector_report.parse_smell(smell)
    )

    assert str(cyclic_fixture_dir / "a.py") in modules
    assert str(cyclic_fixture_dir / "b.py") in modules
    assert "fa" in modules[str(cyclic_fixture_dir / "a.py")]
    assert isinstance(importers_selected, dict)


def test_extract_modules_skips_unknown_participants(cyclic_fixture_dir):
    smell = {
        "File": str(cyclic_fixture_dir / "a.py"),
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
        "Related Participants": json.dumps(
            [
                {"file_path": path, "element_name": "external", "element_type": "module"}
                for path in ("Unknown", "External Library")
            ]
        ),
    }

    modules, importers_selected = generate_proposal.extract_modules_for_refactoring(
        detector_report.parse_smell(smell)
    )

    assert list(modules) == [str(cyclic_fixture_dir / "a.py")]
    assert importers_selected == {}


def test_construct_prompt_orders_primary_first(cyclic_fixture_dir):
    primary = str(cyclic_fixture_dir / "a.py")
    other = str(cyclic_fixture_dir / "b.py")
    smell = {
        "Name": "Cyclic Dependency",
        "File": primary,
        "Snippet": "def fa(): ...",
        "Description": "Cycle",
        "Severity": "Critical",
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
        "Related Participants": json.dumps(
            [
                {"element_name": "module_a", "element_type": "module", "role": "participant", "file_path": primary},
                {"element_name": "module_b", "element_type": "module", "role": "participant", "file_path": other},
            ]
        ),
    }
    modules = {primary: "primary content", other: "other content"}

    prompt = generate_proposal.construct_prompt_for_smell(
        detector_report.parse_smell(smell), modules
    )

    primary_index = prompt.index(primary)
    other_index = prompt.index(other)
    assert primary_index < other_index
    assert "Primary File Snippet" in prompt
    assert "primary content" in prompt


def test_construct_prompt_inserts_placeholder_when_no_snippet():
    smell = {
        "Name": "Cyclic Dependency",
        "File": "tests/fixtures/cyclic_pkg/a.py",
        "Description": "Cycle",
        "Severity": "Critical",
        "Related Participants": "[]",
        "Importers By Participant": "{}",
        "Participant Dependency Edges": "[]",
    }

    prompt = generate_proposal.construct_prompt_for_smell(
        detector_report.parse_smell(smell), modules=None
    )

    assert "# No snippet available" in prompt
