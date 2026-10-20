import csv
import json

import pytest

from scripts import detector_report, filter_smells
from scripts.selection_config import validate_config


def _make_smell(participants):
    if isinstance(participants, str):
        serialized = participants
    else:
        serialized = json.dumps(participants)

    return {
        "Name": "Cyclic Dependency",
        "File": "tests/fixtures/cyclic_pkg/a.py",
        "Line Number": "1",
        "Description": "Cycle between a.py and b.py",
        "Severity": "Critical",
        "Related Participants": serialized,
    }


def test_generate_smell_uuid_is_deterministic():
    participants_a = [
        {"element_name": "A", "element_type": "module", "file_path": "a.py"},
        {"element_name": "B", "element_type": "module", "file_path": "b.py"},
    ]
    participants_b = list(reversed(participants_a))

    smell = _make_smell(participants_a)
    smell_shuffled = _make_smell(participants_b)

    uuid_a = filter_smells.generate_smell_uuid(smell)
    uuid_b = filter_smells.generate_smell_uuid(smell_shuffled)

    assert uuid_a == uuid_b


def test_filter_smells_by_config_filters_names_and_severities():
    smells = [
        {"Name": "Cyclic Dependency", "Severity": "Critical"},
        {"Name": "Potential Redundant Abstractions", "Severity": "minor"},
    ]
    config = {
        "smell_filter": {
            "filter_enabled": True,
            "include_names": ["Cyclic Dependency"],
            "include_severities": ["Critical"],
        }
    }

    smells = [{"row": row} for row in smells]
    filtered = filter_smells.filter_smells_by_config(smells, validate_config(config))

    assert filtered == [smells[0]]


def test_filter_smells_by_config_no_filter_returns_all():
    smells = [
        {"Name": "A", "Severity": "Critical"},
        {"Name": "B", "Severity": "Minor"},
    ]
    config = {"smell_filter": {"filter_enabled": False}}

    smells = [{"row": row} for row in smells]
    filtered = filter_smells.filter_smells_by_config(smells, validate_config(config))

    assert filtered == smells


def test_get_existing_smell_uuids_parses_branch_list():
    uuid_a = "11111111-1111-1111-1111-111111111111"
    uuid_b = "22222222-2222-2222-2222-222222222222"
    branches = f"""
      origin/refactor/{uuid_a}
      upstream/refactor/{uuid_b}
      main
    """

    parsed = filter_smells.get_existing_smell_uuids(branches)

    assert parsed == {uuid_a, uuid_b}


def test_generate_smell_uuid_rejects_bad_participants_json():
    smell = _make_smell(participants="not-json")

    with pytest.raises(ValueError, match="Related Participants"):
        filter_smells.generate_smell_uuid(smell)


def test_deduplicate_cyclic_dependencies_selects_one_per_participant_overlap_group(report_dir):
    """Test that cycle deduplication keeps one representative per participant-overlap group."""
    # Expected UUIDs after deduplication
    expected_uuids = {
        "7d825af9-dab2-5d7f-b1a8-9627f3bbb474",  # non-cycle (redundant abstraction)
        "1259d944-0eb0-509d-9371-ef011f556b95",
        "2311bbde-3855-563a-9470-364b0723bc2b",
        "2b7f90f1-9343-54e3-80c0-25013fdb8548",
        "35d93f5e-fa5d-5039-9de1-2355d915a5ec",
        "41554ee7-9fde-549d-997b-a584d7c0c7f3",
        "523045de-8f32-5184-b97f-5ce0bf3e87da",
        "573c1371-7b21-57b7-93af-3058421c52fa",  # GIS participant-overlap group rep
        "598dcba8-c7e0-5c17-a24b-a0077c47688d",
        "90ba9c05-5281-5481-af42-17e590bb28bb",
        "9ceda21f-834e-51af-b6f7-af9ed54591b9",
        "cff24e2c-e57a-55a1-8618-d08bc8eb1a44",
        "d8d16bc0-e965-5f9f-81f6-84f062f6bb9a",
        "ff1e69a9-64cb-569b-93a2-fcb109b98067",
    }

    # Load test CSV with redundant cycles
    with open(report_dir / "cycles_report.csv") as f:
        reader = csv.DictReader(f)
        smells = [
            detector_report.parse_smell({
                "Importers By Participant": "{}",
                "Participant Dependency Edges": "[]",
                **row,
            })
            for row in reader
        ]

    # Run deduplication
    result = filter_smells.deduplicate_cyclic_dependencies(smells)

    # Verify count
    assert len(result) == 14

    # Verify expected UUIDs
    result_uuids = {smell["row"]["UUID"] for smell in result}
    assert result_uuids == expected_uuids

    # Verify GIS participant-overlap representative has expected signature
    gis_rep = next(r for r in result if r["row"]["UUID"] == "573c1371-7b21-57b7-93af-3058421c52fa")
    mods = sorted({filter_smells.normalize_path(p["file_path"]) for p in gis_rep["participants"]})
    expected_gis_sig = "django/contrib/gis/geos/collections.py||django/contrib/gis/geos/geometry.py||django/contrib/gis/geos/linestring.py"
    assert "||".join(mods) == expected_gis_sig


def test_select_top_smells():
    """Test round-robin selection with severity prioritization."""
    smells = [
        {"Name": "Cyclic Dependency", "UUID": "cd-1", "Severity": "Low"},
        {"Name": "Cyclic Dependency", "UUID": "cd-2", "Severity": "High"},
        {"Name": "Cyclic Dependency", "UUID": "cd-3", "Severity": "Medium"},
        {"Name": "Potential Redundant Abstractions", "UUID": "pra-1", "Severity": "Medium"},
        {"Name": "Potential Redundant Abstractions", "UUID": "pra-2", "Severity": "High"},
    ]
    config = {"max_proposal_limit": 3}

    smells = [{"row": row} for row in smells]
    result = filter_smells.select_top_smells(smells, validate_config(config))

    assert len(result) == 3
    # Round-robin picks highest severity from each type first:
    # Round 1: CD (High=cd-2), PRA (High=pra-2)
    # Round 2: CD (Medium=cd-3)
    assert result[0]["row"]["UUID"] == "cd-2"  # CD High
    assert result[1]["row"]["UUID"] == "pra-2"  # PRA High
    assert result[2]["row"]["UUID"] == "cd-3"  # CD Medium


def test_filter_smells_by_importer_count_excludes_over_limit_smells():
    over_limit = {
        "UUID": "over-limit",
        "Importers By Participant": json.dumps({
            "pkg/a.py": [
                f"pkg/importer_{i}.py"
                for i in range(filter_smells.MAX_IMPORTERS + 1)
            ],
        }),
    }
    within_limit = {
        "UUID": "within-limit",
        "Importers By Participant": json.dumps({
            "pkg/a.py": [
                f"pkg/importer_{i}.py"
                for i in range(filter_smells.MAX_IMPORTERS)
            ],
        }),
    }

    smells = [
        {"row": row, "importers": detector_report.parse_importers_by_participant(row)}
        for row in (over_limit, within_limit)
    ]
    filtered = filter_smells.filter_smells_by_importer_count(smells)

    assert [s["row"]["UUID"] for s in filtered] == ["within-limit"]


class TestParseImportersByParticipant:
    """Tests for parse_importers_by_participant()."""

    def test_valid_json_returns_normalized_paths(self):
        row = {
            "Importers By Participant": json.dumps({
                "./src/a.py": ["./src/main.py", "./src/utils.py"],
                "src\\b.py": ["src\\client.py"],
            })
        }
        result = detector_report.parse_importers_by_participant(row)

        assert result == {
            "src/a.py": ["src/main.py", "src/utils.py"],
            "src/b.py": ["src/client.py"],
        }

    def test_deduplicates_and_sorts_importers(self):
        row = {
            "Importers By Participant": json.dumps({
                "a.py": ["z.py", "a.py", "m.py", "z.py"],
            })
        }
        result = detector_report.parse_importers_by_participant(row)

        # Should be sorted and deduplicated
        assert result == {"a.py": ["a.py", "m.py", "z.py"]}

    def test_non_array_importers_raise_value_error(self):
        row = {
            "Importers By Participant": json.dumps({
                "a.py": "client.py",
            })
        }
        with pytest.raises(ValueError, match="expected list"):
            detector_report.parse_importers_by_participant(row)

    def test_merges_importers_for_equivalent_normalized_participants(self):
        row = {
            "Importers By Participant": json.dumps({
                "./pkg/a.py": ["pkg/main.py", "pkg/api.py"],
                "pkg/a.py": ["pkg/worker.py", "pkg/jobs.py"],
            })
        }
        result = detector_report.parse_importers_by_participant(row)

        assert result == {
            "pkg/a.py": ["pkg/api.py", "pkg/jobs.py", "pkg/main.py", "pkg/worker.py"]
        }

    def test_explicit_empty_dict_is_valid(self):
        assert detector_report.parse_importers_by_participant({"Importers By Participant": "{}"}) == {}

    def test_malformed_json_raises_with_smell_context(self):
        row = {
            "UUID": "test-uuid",
            "Importers By Participant": "not-valid-json",
        }
        with pytest.raises(ValueError, match="Importers By Participant.*test-uuid.*malformed JSON"):
            detector_report.parse_importers_by_participant(row)

    def test_rejects_empty_paths(self):
        row = {
            "Importers By Participant": json.dumps({
                "a.py": ["b.py", "", None, "c.py"],
                "": ["d.py"],
            })
        }
        with pytest.raises(ValueError, match="nonempty string"):
            detector_report.parse_importers_by_participant(row)


class TestParseDependencyEdges:
    """Tests for parse_dependency_edges()."""

    def test_valid_json_returns_normalized_edges(self):
        row = {
            "Participant Dependency Edges": json.dumps([
                ["./src/a.py", "./src/b.py"],
                ["src\\b.py", "src\\a.py"],
            ])
        }
        result = detector_report.parse_dependency_edges(row)

        assert result == [
            ("src/a.py", "src/b.py"),
            ("src/b.py", "src/a.py"),
        ]

    def test_deduplicates_and_sorts_edges(self):
        row = {
            "Participant Dependency Edges": json.dumps([
                ["z.py", "a.py"],
                ["a.py", "b.py"],
                ["z.py", "a.py"],  # duplicate
            ])
        }
        result = detector_report.parse_dependency_edges(row)

        # Should be sorted and deduplicated
        assert result == [("a.py", "b.py"), ("z.py", "a.py")]

    def test_explicit_empty_list_is_valid(self):
        assert detector_report.parse_dependency_edges({"Participant Dependency Edges": "[]"}) == []

    def test_malformed_json_raises_with_smell_context(self):
        row = {
            "File": "test-file.py",
            "Participant Dependency Edges": "not-valid-json",
        }
        with pytest.raises(ValueError, match="Participant Dependency Edges.*test-file.py.*malformed JSON"):
            detector_report.parse_dependency_edges(row)

    def test_rejects_invalid_edges(self):
        row = {
            "Participant Dependency Edges": json.dumps([
                ["a.py", "b.py"],  # valid
                ["only-one"],      # invalid: not 2 elements
                ["", "b.py"],      # invalid: empty src
                ["a.py", ""],      # invalid: empty dst
                "not-a-list",      # invalid: not a list/tuple
            ])
        }
        with pytest.raises(ValueError, match="edge 1 must be a pair"):
            detector_report.parse_dependency_edges(row)
