"""Exercise persisted validation evidence and its PR rendering without models."""

import json
from types import SimpleNamespace

import pytest

from scripts import correctness_filter
from scripts.generate_pr_description import render_validation_assessment
from scripts.correctness_filter import CHECK_IDS


@pytest.fixture
def candidate(tmp_path, monkeypatch, redundant_fixture_dir):
    """A merge with executed tests, a retained canonical file, and a safe deletion."""
    source = (redundant_fixture_dir / "base.py").read_text()
    modules = {"canonical.py": source, "obsolete.py": source}
    operations = [
        {"path": "canonical.py", "operation": "modify", "content": source},
        {"path": "obsolete.py", "operation": "delete", "content": None},
    ]
    proposal = {"strategy": "MergeModules", "canonical_module": "canonical.py"}
    for filename, data in (
        ("modules.json", modules),
        ("refactored_files.json", operations),
        ("proposal.json", proposal),
    ):
        (tmp_path / filename).write_text(json.dumps(data))
    (tmp_path / "canonical.py").write_text(source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setenv("SIMILARITY_THRESHOLD", "0.85")

    calls = []

    def run_pytest(command, **kwargs):
        assert command == ["pytest"]
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout=b"1 passed", stderr=b"")

    def similarity_inputs(*args, **kwargs):
        calls.append("similarity_inputs")
        return source, source

    def similarity(candidate_source, reference_source, threshold):
        calls.append("score")
        assert candidate_source == reference_source == source
        return 0.93 >= threshold, 0.93

    monkeypatch.setattr(correctness_filter.subprocess, "run", run_pytest)
    # Whole-file selection has separate adapter tests; this suite exercises the
    # assessment boundary, real strategy rules, and real filesystem import scans.
    monkeypatch.setattr(correctness_filter, "_load_similarity_inputs", similarity_inputs)
    monkeypatch.setattr(correctness_filter, "check_similarity", similarity)
    return SimpleNamespace(
        root=tmp_path, calls=calls, proposal=proposal, operations=operations
    )


def run_and_read(candidate, expected_exit):
    assert correctness_filter.main() == expected_exit
    serialized = (candidate.root / "validation_assessment.json").read_text()
    assessment = json.loads(serialized)
    assert set(assessment["checks"]) == set(CHECK_IDS)
    assert all(check["reason"].strip() for check in assessment["checks"].values())
    assert serialized == json.dumps(assessment, indent=2, sort_keys=True) + "\n"
    body = render_validation_assessment(assessment)
    assert f"**Overall decision:** {assessment['decision'].capitalize()}" in body
    for check in assessment["checks"].values():
        assert check["reason"] in body
    expected_labels = "refactoring, automated-pr"
    if (
        assessment["decision"] == "accepted"
        and assessment["checks"]["repository_tests"]["status"] == "skipped"
    ):
        expected_labels += ", no-tests"
    assert (candidate.root / "github_output").read_text() == (
        f"pr_labels={expected_labels}\n"
    )
    return assessment, body


def test_accepted_candidate_records_and_renders_all_executed_checks(candidate):
    assessment, body = run_and_read(candidate, 0)

    assert assessment["decision"] == "accepted"
    assert {check["status"] for check in assessment["checks"].values()} == {"passed"}
    assert assessment["checks"]["code_similarity"].get("score") == 0.93
    assert assessment["checks"]["code_similarity"].get("threshold") == 0.85
    assert (candidate.root / "github_output").read_text() == (
        "pr_labels=refactoring, automated-pr\n"
    )
    assert "**Repository tests:** Passed" in body
    assert "**Strategy policy:** Passed" in body
    assert "**Deletion safety:** Passed" in body
    assert "**Code similarity:** Passed (score: 0.9300; threshold: 0.8500)" in body


@pytest.mark.parametrize(
    ("test_outcome", "reason"),
    [
        (5, "No tests collected"),
        ("missing", "pytest not found"),
    ],
)
def test_accepted_skipped_tests_are_never_reported_as_passed(
    candidate, monkeypatch, test_outcome, reason
):
    def run_pytest(*args, **kwargs):
        if test_outcome == "missing":
            raise FileNotFoundError("pytest")
        return SimpleNamespace(returncode=test_outcome, stdout=b"", stderr=b"")

    monkeypatch.setattr(correctness_filter.subprocess, "run", run_pytest)
    assessment, body = run_and_read(candidate, 0)

    assert assessment["decision"] == "accepted"
    assert assessment["checks"]["repository_tests"]["status"] == "skipped"
    assert reason in assessment["checks"]["repository_tests"]["reason"]
    assert (candidate.root / "github_output").read_text() == (
        "pr_labels=refactoring, automated-pr, no-tests\n"
    )
    assert "**Repository tests:** Skipped" in body
    assert "**Repository tests:** Passed" not in body
    assert assessment["checks"]["code_similarity"]["status"] == "passed"


def test_rejection_after_skipped_tests_uses_completed_assessment_for_labels(
    candidate, monkeypatch
):
    monkeypatch.setattr(
        correctness_filter.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=5, stdout=b"", stderr=b""),
    )
    candidate.proposal["canonical_module"] = "obsolete.py"
    (candidate.root / "proposal.json").write_text(json.dumps(candidate.proposal))

    assessment, body = run_and_read(candidate, 1)

    assert assessment["decision"] == "rejected"
    assert assessment["checks"]["repository_tests"]["status"] == "skipped"
    assert assessment["checks"]["strategy_policy"]["status"] == "failed"
    assert "**Repository tests:** Skipped" in body
    assert (candidate.root / "github_output").read_text() == (
        "pr_labels=refactoring, automated-pr\n"
    )


@pytest.mark.parametrize(
    ("proposal_present", "strategy_status", "strategy_label"),
    [(False, "skipped", "Skipped"), (True, "not_applicable", "Not applicable")],
)
def test_skipped_and_inapplicable_checks_are_recorded_and_rendered_distinctly(
    candidate, monkeypatch, proposal_present, strategy_status, strategy_label
):
    proposal_path = candidate.root / "proposal.json"
    if proposal_present:
        proposal_path.write_text(json.dumps({"strategy": "ExtractUtility"}))
    else:
        proposal_path.unlink()
    (candidate.root / "refactored_files.json").write_text("[]")
    monkeypatch.setattr(
        correctness_filter, "_load_similarity_inputs", lambda *a, **kw: ("", "")
    )

    assessment, body = run_and_read(candidate, 0)

    assert assessment["decision"] == "accepted"
    assert assessment["checks"]["repository_tests"]["status"] == "passed"
    assert assessment["checks"]["strategy_policy"]["status"] == strategy_status
    assert assessment["checks"]["strategy_policy"].get("score") is None
    assert f"**Strategy policy:** {strategy_label}" in body
    assert "**Strategy policy:** Passed" not in body
    for name in CHECK_IDS[2:]:
        assert assessment["checks"][name]["status"] == "not_applicable"
        assert assessment["checks"][name].get("score") is None
    for label in ("Deletion safety", "Code similarity"):
        assert f"**{label}:** Not applicable" in body
        assert f"**{label}:** Passed" not in body
    assert candidate.calls == []


@pytest.mark.parametrize(
    "failed_check", ["repository_tests", "strategy_policy", "deletion_safety"]
)
def test_rejection_leaves_later_checks_not_reached(candidate, monkeypatch, failed_check):
    if failed_check == "repository_tests":
        monkeypatch.setattr(
            correctness_filter.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b"failed", stderr=b""),
        )
    elif failed_check == "strategy_policy":
        candidate.proposal["canonical_module"] = "obsolete.py"
        (candidate.root / "proposal.json").write_text(json.dumps(candidate.proposal))
    else:
        (candidate.root / "consumer.py").write_text("from obsolete import do_thing\n")

    assessment, body = run_and_read(candidate, 1)

    assert assessment["decision"] == "rejected"
    failed_index = CHECK_IDS.index(failed_check)
    assert assessment["checks"][failed_check]["status"] == "failed"
    for name in CHECK_IDS[:failed_index]:
        assert assessment["checks"][name]["status"] == "passed"
    for name in CHECK_IDS[failed_index + 1 :]:
        assert assessment["checks"][name]["status"] == "not_reached"
    assert "**Code similarity:** Not reached" in body
    assert "**Code similarity:** Passed" not in body
    assert candidate.calls == []


@pytest.mark.parametrize("failed_check", ["repository_tests", "deletion_safety"])
def test_runtime_failure_is_attributed_and_stops_later_checks(
    candidate, monkeypatch, failed_check
):
    def runtime_failure(*args, **kwargs):
        raise OSError("Local validation resource unavailable")

    if failed_check == "repository_tests":
        monkeypatch.setattr(correctness_filter.subprocess, "run", runtime_failure)
    else:
        monkeypatch.setattr(correctness_filter, "check_deletion_safety", runtime_failure)

    assessment, body = run_and_read(candidate, 1)

    failed_index = CHECK_IDS.index(failed_check)
    result = assessment["checks"][failed_check]
    assert result["status"] == "failed"
    assert result["reason"] == "OSError: Local validation resource unavailable"
    for name in CHECK_IDS[:failed_index]:
        assert assessment["checks"][name]["status"] == "passed"
    for name in CHECK_IDS[failed_index + 1 :]:
        assert assessment["checks"][name]["status"] == "not_reached"
    assert "**Code similarity:** Not reached" in body
    assert candidate.calls == []


@pytest.mark.parametrize(
    ("score", "status", "exit_code"), [(0.9, "passed", 0), (0.89, "failed", 1)]
)
def test_similarity_preserves_score_and_configured_threshold(
    candidate, monkeypatch, score, status, exit_code
):
    monkeypatch.setenv("SIMILARITY_THRESHOLD", "0.9")
    monkeypatch.setattr(
        correctness_filter,
        "check_similarity",
        lambda candidate, reference, threshold: (score >= threshold, score),
    )

    assessment, body = run_and_read(candidate, exit_code)

    result = assessment["checks"]["code_similarity"]
    assert result["status"] == status
    assert result.get("score") == score
    assert result.get("threshold") == 0.9
    assert (
        f"**Code similarity:** {status.capitalize()} "
        f"(score: {score:.4f}; threshold: 0.9000)"
    ) in body


@pytest.mark.parametrize("filename", ["modules.json", "refactored_files.json"])
@pytest.mark.parametrize("corruption", ["missing", "invalid_json"])
def test_input_errors_still_leave_diagnostic_artifact(candidate, filename, corruption):
    path = candidate.root / filename
    if corruption == "missing":
        path.unlink()
    else:
        path.write_text("{ invalid JSON")

    assessment, body = run_and_read(candidate, 1)

    assert assessment["decision"] == "rejected"
    assert "validation input loading" in assessment["reason"]
    assert assessment["checks"]["repository_tests"]["status"] == "passed"
    assert all(
        assessment["checks"][name]["status"] == "not_reached"
        for name in CHECK_IDS[1:]
    )
    assert "**Strategy policy:** Not reached" in body
    assert candidate.calls == []


def test_malformed_proposal_is_failed_with_later_checks_not_reached(candidate):
    (candidate.root / "proposal.json").write_text("[]")

    assessment, body = run_and_read(candidate, 1)

    assert assessment["checks"]["strategy_policy"]["status"] == "failed"
    assert "AttributeError" in assessment["checks"]["strategy_policy"]["reason"]
    assert assessment["checks"]["deletion_safety"]["status"] == "not_reached"
    assert assessment["checks"]["code_similarity"]["status"] == "not_reached"
    assert "**Strategy policy:** Failed" in body
    assert candidate.calls == []


def test_scoring_failure_retains_threshold_in_diagnostic_artifact(candidate, monkeypatch):
    def unavailable_score(*args):
        raise RuntimeError("Local scorer unavailable")

    monkeypatch.setattr(correctness_filter, "check_similarity", unavailable_score)
    assessment, body = run_and_read(candidate, 1)

    result = assessment["checks"]["code_similarity"]
    assert result["status"] == "failed"
    assert result["reason"] == "RuntimeError: Local scorer unavailable"
    assert result.get("score") is None
    assert result.get("threshold") == 0.85
    assert "**Code similarity:** Failed (threshold: 0.8500)" in body
    assert "score:" not in body


@pytest.mark.parametrize("score", [float("nan"), float("inf")])
def test_nonfinite_score_preserves_serializable_failure_evidence(
    candidate, monkeypatch, score
):
    monkeypatch.setattr(
        correctness_filter,
        "check_similarity",
        lambda *args: (True, score),
    )

    assessment, body = run_and_read(candidate, 1)

    result = assessment["checks"]["code_similarity"]
    assert result["status"] == "failed"
    assert "must be finite numbers" in result["reason"]
    assert result.get("score") is None
    assert result.get("threshold") == 0.85
    assert "**Code similarity:** Failed (threshold: 0.8500)" in body
    assert "score:" not in body


@pytest.mark.parametrize("threshold", ["not-a-number", "nan"])
def test_invalid_similarity_threshold_preserves_failure_without_invalid_metrics(
    candidate, monkeypatch, threshold
):
    monkeypatch.setenv("SIMILARITY_THRESHOLD", threshold)

    assessment, body = run_and_read(candidate, 1)

    result = assessment["checks"]["code_similarity"]
    assert result["status"] == "failed"
    assert "ValueError" in result["reason"]
    assert result.get("score") is None
    assert result.get("threshold") is None
    assert "**Code similarity:** Failed" in body
    assert "score:" not in body
    assert "threshold:" not in body
