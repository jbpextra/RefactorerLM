"""Build complete source comparisons from original and candidate files."""

import json

import pytest

from scripts.correctness_filter import build_similarity_inputs


@pytest.fixture
def load_case(fixtures_dir):
    def load(name):
        return json.loads((fixtures_dir / "similarity_cases.json").read_text())[name]

    return load


def build_case(case):
    details = case["smell_details"]
    participants = {details["File"]} | {
        item["file_path"] for item in json.loads(details["Related Participants"])
    }
    changed = set(case["changed_paths"])
    operations = {
        item["path"]: item["operation"]
        for item in case["refactored"]
        if item["path"] in changed
    }
    return build_similarity_inputs(
        case["modules"],
        case["candidate_sources"],
        participants,
        operations,
    )


@pytest.mark.parametrize("missing", ["original", "candidate"])
def test_missing_source_does_not_mean_an_empty_file_or_permitted_skip(missing):
    with pytest.raises(KeyError, match="a.py"):
        build_similarity_inputs(
            {} if missing == "original" else {"a.py": ""},
            {} if missing == "candidate" else {"a.py": ""},
            {"a.py"},
            {},
        )


def test_empty_files_are_present_and_have_markers():
    before, after = build_similarity_inputs(
        {"a.py": ""}, {"a.py": ""}, {"a.py"}, {}
    )
    assert before == after == "# FILE: a.py\n"


def test_creation_cannot_replace_an_original_participant():
    with pytest.raises(ValueError):
        build_similarity_inputs(
            {"a.py": "old"}, {"a.py": "actual"}, {"a.py"}, {"a.py": "create"}
        )


def test_consolidation_includes_unchanged_survivor_and_full_modified_caller(load_case):
    case = load_case("consolidation")
    before, after = build_case(case)
    assert case["modules"]["shared_rules.py"] in before
    assert case["candidate_sources"]["shared_rules.py"] in after
    assert case["modules"]["service.py"] in before
    assert case["candidate_sources"]["service.py"] in after
    assert "def summarize_request(items, request_id):" in after
    assert 'raise ValueError("request_id is required")' in after
    assert "# FILE: unrelated_report.py" not in before
    assert "# FILE: unrelated_report.py" not in after
    assert "def unrelated_report" not in after


def test_deleted_and_created_files_appear_only_on_their_existing_side(load_case):
    before, after = build_case(load_case("consolidation"))
    assert "# FILE: legacy_rules.py\n" in before
    assert "# FILE: legacy_rules.py\n" not in after
    extraction_case = load_case("extraction")
    before, after = build_case(extraction_case)
    assert "# FILE: helpers.py\n" not in before
    assert "# FILE: helpers.py\n" in after
    assert extraction_case["candidate_sources"]["helpers.py"] in after
    assert extraction_case["modules"]["service.py"] in before
    assert extraction_case["candidate_sources"]["service.py"] in after


@pytest.mark.parametrize(
    ("case_name", "path", "defect"),
    [
        (
            "incorrect_redirection",
            "service.py",
            "result = shared_rules.summarize([])",
        ),
        ("changed_calculation", "shared_rules.py", "average = total * count"),
        (
            "changed_exception",
            "shared_rules.py",
            'return {"error": "negative amount"}',
        ),
    ],
)
def test_defects_are_exposed_from_actual_candidate_not_operation_content(
    load_case, case_name, path, defect
):
    case = load_case(case_name)
    # Operation content is deliberately stale. The candidate is authoritative.
    operation = next(item for item in case["refactored"] if item["path"] == path)
    operation["content"] = load_case("consolidation")["candidate_sources"][path]
    before, after = build_case(case)
    assert case["candidate_sources"][path] in after
    assert defect in after
    assert defect not in before
    assert case["modules"][path] in before


def test_created_helper_uses_actual_candidate_source(load_case):
    case = load_case("extraction")
    case["candidate_sources"]["helpers.py"] = case["candidate_sources"][
        "helpers.py"
    ].replace("average = total / count", "average = total * count")
    before, after = build_case(case)
    assert "average = total * count" in after
    assert "average = total / count" not in after
    assert "average = total / count" in before


def test_identical_participant_text_keeps_distinct_file_entries(load_case):
    case = load_case("consolidation")
    before, after = build_case(case)
    shared_source = case["modules"]["shared_rules.py"]
    assert case["modules"]["legacy_rules.py"] == shared_source
    assert before.count(shared_source) == 2
    assert before.count("# FILE: legacy_rules.py\n") == 1
    assert before.count("# FILE: shared_rules.py\n") == 1
    assert after.count(shared_source) == 1


def test_equivalent_input_orders_serialize_identically(load_case):
    case = load_case("consolidation")
    expected = build_case(case)
    case["modules"] = dict(reversed(list(case["modules"].items())))
    case["candidate_sources"] = dict(reversed(list(case["candidate_sources"].items())))
    case["refactored"].reverse()
    case["changed_paths"].reverse()
    case["smell_details"]["Related Participants"] = json.dumps(
        list(reversed(json.loads(case["smell_details"]["Related Participants"])))
    )
    assert build_case(case) == expected
