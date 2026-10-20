import csv
import json
import sys

import pytest

from scripts import generate_pr_description
from scripts.smell_identity import generate_smell_uuid


@pytest.fixture
def pr_inputs(tmp_path):
    row = {
        "Name": "Cyclic Dependency",
        "File": "package/a.py",
        "Line Number": "1",
        "Description": "Cycle between a and b",
        "Severity": "Critical",
        "Related Participants": "[]",
    }
    report = tmp_path / "filtered_report.csv"
    with report.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps({"strategy": "ExtractUtility", "explanation": "Share the utility."}),
        encoding="utf-8",
    )
    return {
        "smell_uuid": generate_smell_uuid(row),
        "report_file": report,
        "proposal_file": proposal,
    }


def accepted_assessment():
    return {
        "decision": "accepted",
        "reason": "All required validation checks succeeded.",
        "checks": {
            "repository_tests": {"status": "passed", "reason": "pytest exited with 0."},
            "strategy_policy": {
                "status": "passed",
                "reason": "The strategy permits these operations.",
            },
            "deletion_safety": {
                "status": "passed",
                "reason": "No references to deleted modules remain.",
            },
            "code_similarity": {
                "status": "passed",
                "reason": "Similarity meets the acceptance threshold.",
                "score": 0.9123,
                "threshold": 0.85,
            },
        },
    }


def render_assessment(tmp_path, pr_inputs, assessment):
    assessment_file = tmp_path / "validation_assessment.json"
    assessment_file.write_text(json.dumps(assessment), encoding="utf-8")
    return generate_pr_description.generate_pr_description(
        **pr_inputs, validation_assessment_file=assessment_file
    )


@pytest.mark.parametrize(
    ("actions", "expected_lines"),
    [
        (
            ["merge_module", "modify_module", "remove_module"],
            ["1. **merge_module**", "2. **modify_module**", "3. **remove_module**"],
        ),
        (
            [{"type": "modify_module", "details": "Preserve the existing assertions."}],
            ["1. **modify_module**", "   - Preserve the existing assertions."],
        ),
        (
            [
                {"op": "merge_module", "source": "old.py", "target": "canonical.py"},
                {"op": "modify_module", "details": "Preserve the existing assertions."},
                {"op": "remove_module", "module": "old.py"},
            ],
            [
                "1. **merge_module**",
                "2. **modify_module**",
                "   - Preserve the existing assertions.",
                "3. **remove_module**",
            ],
        ),
    ],
    ids=["atomic_operations", "detailed_operations", "op_dictionaries"],
)
def test_pr_renders_proposal_actions(tmp_path, pr_inputs, actions, expected_lines):
    proposal_file = pr_inputs["proposal_file"]
    proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
    proposal["actions"] = actions
    proposal_file.write_text(json.dumps(proposal), encoding="utf-8")

    _, body = render_assessment(tmp_path, pr_inputs, accepted_assessment())

    assert "### Actions Planned" in body
    assert "**unknown**" not in body
    for line in expected_lines:
        assert line in body.splitlines()
    assert "**Overall decision:** Accepted" in body


def test_pr_renders_action_prose_without_file_contents(pr_inputs):
    proposal = {
        "strategy": "MergeModules",
        "actions": [
            {
                "type": "merge_module",
                "description": "Merge old.py into canonical.py.",
                "rationale": "Keep the shared behavior in one module.",
            },
            {
                "op": "modify_module",
                "description": "Update client.py to import canonical.py.",
                "rationale": "Preserve callers after removing old.py.",
                "details": {"modify_file": {"content": "NESTED_SOURCE_CONTENT"}},
                "content": "FULL_SOURCE_CONTENT",
                "diff": "SOURCE_DIFF",
            },
            {
                "operation": "remove_module",
                "rationale": "Remove old.py once its callers use canonical.py.",
                "details": {"content": "STRUCTURED_DETAILS_CONTENT"},
            },
        ],
    }
    pr_inputs["proposal_file"].write_text(json.dumps(proposal), encoding="utf-8")

    _, body = generate_pr_description.generate_pr_description(**pr_inputs)

    assert (
        "1. **merge_module**\n"
        "   - Merge old.py into canonical.py.\n"
        "   - **Rationale:** Keep the shared behavior in one module.\n"
        "2. **modify_module**\n"
        "   - Update client.py to import canonical.py.\n"
        "   - **Rationale:** Preserve callers after removing old.py.\n"
        "3. **remove_module**\n"
        "   - **Rationale:** Remove old.py once its callers use canonical.py."
    ) in body
    for omitted in (
        "NESTED_SOURCE_CONTENT",
        "FULL_SOURCE_CONTENT",
        "SOURCE_DIFF",
        "STRUCTURED_DETAILS_CONTENT",
    ):
        assert omitted not in body


def test_pr_keeps_action_prose_short_and_on_one_line(pr_inputs):
    description = "Update imports. " * 30
    rationale = "Preserve callers. " * 30
    pr_inputs["proposal_file"].write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "type": "modify_module",
                        "description": "\n\t" + description,
                        "rationale": rationale,
                        "details": "Do not repeat legacy details.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _, body = generate_pr_description.generate_pr_description(**pr_inputs)

    assert f"   - {description[:237]}..." in body.splitlines()
    assert f"   - **Rationale:** {rationale[:237]}..." in body.splitlines()
    assert description not in body
    assert rationale not in body
    assert "Do not repeat legacy details." not in body


def test_pr_reports_executed_checks_and_similarity_metrics(tmp_path, pr_inputs):
    title, body = render_assessment(tmp_path, pr_inputs, accepted_assessment())

    assert title == "Refactor: Fix Cyclic Dependency using ExtractUtility strategy"
    assert "**Overall decision:** Accepted" in body
    assert "**Reason:** All required validation checks succeeded." in body
    assert "- **Repository tests:** Passed. pytest exited with 0." in body
    assert "- **Strategy policy:** Passed. The strategy permits these operations." in body
    assert "- **Deletion safety:** Passed. No references to deleted modules remain." in body
    assert (
        "- **Code similarity:** Passed (score: 0.9123; threshold: 0.8500). "
        "Similarity meets the acceptance threshold."
    ) in body
    assert "Tests pass (automated correctness check passed)" not in body


@pytest.mark.parametrize(
    "reason",
    ["No tests collected; test validation was skipped.", "pytest is unavailable."],
)
def test_pr_reports_skipped_tests_without_a_pass_claim(tmp_path, pr_inputs, reason):
    assessment = accepted_assessment()
    assessment["reason"] = "Accepted under the existing policy with test validation skipped."
    assessment["checks"]["repository_tests"] = {"status": "skipped", "reason": reason}

    _, body = render_assessment(tmp_path, pr_inputs, assessment)

    assert "**Overall decision:** Accepted" in body
    assert f"- **Repository tests:** Skipped. {reason}" in body
    assert "Repository tests:** Passed" not in body
    assert "Tests pass" not in body


def test_pr_reports_inapplicable_checks_distinctly(tmp_path, pr_inputs):
    assessment = accepted_assessment()
    assessment["checks"]["strategy_policy"] = {
        "status": "not_applicable",
        "reason": "No deletion restrictions apply to this strategy.",
    }
    assessment["checks"]["deletion_safety"] = {
        "status": "not_applicable",
        "reason": "The candidate does not delete files.",
    }
    assessment["checks"]["code_similarity"] = {
        "status": "not_applicable",
        "reason": "No comparable original sources are available.",
    }

    _, body = render_assessment(tmp_path, pr_inputs, assessment)

    for label in ("Strategy policy", "Deletion safety", "Code similarity"):
        assert f"- **{label}:** Not applicable." in body
        assert f"{label}:** Passed" not in body
    assert "The candidate does not delete files." in body
    assert "score:" not in body


def test_pr_reports_rejection_and_later_checks_not_reached(tmp_path, pr_inputs):
    assessment = accepted_assessment()
    assessment["decision"] = "rejected"
    assessment["reason"] = "Repository tests failed."
    assessment["checks"]["repository_tests"] = {
        "status": "failed",
        "reason": "pytest exited with 1.",
    }
    for name in ("strategy_policy", "deletion_safety", "code_similarity"):
        assessment["checks"][name] = {
            "status": "not_reached",
            "reason": "Validation stopped after repository tests failed.",
        }

    _, body = render_assessment(tmp_path, pr_inputs, assessment)

    assert "**Overall decision:** Rejected" in body
    assert "**Reason:** Repository tests failed." in body
    assert "- **Repository tests:** Failed. pytest exited with 1." in body
    for label in ("Strategy policy", "Deletion safety", "Code similarity"):
        assert f"- **{label}:** Not reached." in body
    assert "Passed" not in body
    assert "score:" not in body


def test_pr_without_assessment_reports_no_validation_claim(pr_inputs):
    _, body = generate_pr_description.generate_pr_description(**pr_inputs)

    assert "Validation assessment unavailable; no check outcomes are reported." in body
    assert "Tests pass" not in body
    assert "**Overall decision:** Accepted" not in body


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"status": "probably_passed"},
        {"reason": " "},
        {"status": "skipped"},
        {"score": float("nan")},
        {"threshold": float("inf")},
        {"score": True},
        {"threshold": None},
    ],
)
def test_pr_rejects_malformed_check_result(tmp_path, pr_inputs, invalid_result):
    assessment = accepted_assessment()
    assessment["checks"]["code_similarity"].update(invalid_result)

    with pytest.raises(ValueError):
        render_assessment(tmp_path, pr_inputs, assessment)


@pytest.mark.parametrize("assessment", [None, [], {}])
def test_pr_rejects_invalid_assessment_shape(tmp_path, pr_inputs, assessment):
    with pytest.raises(ValueError):
        render_assessment(tmp_path, pr_inputs, assessment)


@pytest.mark.parametrize("status", ["failed", "not_reached"])
def test_pr_rejects_accepted_assessment_with_failed_or_unreached_check(
    tmp_path, pr_inputs, status
):
    assessment = accepted_assessment()
    assessment["checks"]["repository_tests"]["status"] = status

    with pytest.raises(ValueError):
        render_assessment(tmp_path, pr_inputs, assessment)


def test_pr_rejects_missing_check(tmp_path, pr_inputs):
    assessment = accepted_assessment()
    del assessment["checks"]["strategy_policy"]

    with pytest.raises(ValueError):
        render_assessment(tmp_path, pr_inputs, assessment)


def test_pr_rejects_empty_overall_reason(tmp_path, pr_inputs):
    assessment = accepted_assessment()
    assessment["reason"] = ""

    with pytest.raises(ValueError):
        render_assessment(tmp_path, pr_inputs, assessment)


def test_pr_reports_threshold_when_similarity_cannot_produce_score(tmp_path, pr_inputs):
    assessment = accepted_assessment()
    assessment["decision"] = "rejected"
    assessment["reason"] = "Similarity calculation failed."
    assessment["checks"]["code_similarity"] = {
        "status": "failed",
        "reason": "The scorer is unavailable.",
        "threshold": 0.85,
    }

    _, body = render_assessment(tmp_path, pr_inputs, assessment)

    assert "- **Code similarity:** Failed (threshold: 0.8500)." in body
    assert "score:" not in body


def test_cli_passes_assessment_to_rendering(tmp_path, pr_inputs, monkeypatch):
    assessment_file = tmp_path / "validation_assessment.json"
    assessment_file.write_text(json.dumps(accepted_assessment()), encoding="utf-8")
    body_file = tmp_path / "pr_body.txt"
    title_file = tmp_path / "pr_title.txt"
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_pr_description.py",
            "--smell-uuid",
            pr_inputs["smell_uuid"],
            "--report-file",
            str(pr_inputs["report_file"]),
            "--proposal-file",
            str(pr_inputs["proposal_file"]),
            "--validation-assessment",
            str(assessment_file),
            "--output-title",
            str(title_file),
            "--output-body",
            str(body_file),
        ],
    )

    generate_pr_description.main()

    assert "**Overall decision:** Accepted" in body_file.read_text(encoding="utf-8")
    assert "Repository tests:** Passed" in body_file.read_text(encoding="utf-8")
    assert title_file.read_text(encoding="utf-8").startswith("Refactor: Fix Cyclic Dependency")
