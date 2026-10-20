import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest
import yaml

from scripts.smell_identity import generate_smell_uuid


WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github/workflows/refactorer.yml"


@pytest.fixture
def refactoring_steps():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]["refactor-smells"]["steps"]


def test_validation_assessment_is_rendered_and_retained_before_publication(
    refactoring_steps,
):
    names = [step["name"] for step in refactoring_steps]
    steps = {step["name"]: step for step in refactoring_steps}
    assert names.index("Run correctness filter") < names.index("Generate PR description")
    assert names.index("Generate PR description") < names.index(
        "Upload refactoring artifacts"
    ) < names.index("Create Pull Request")

    # Rejected assessments still receive a rendered diagnostic body.
    assert steps["Generate PR description"]["if"] == "env.CHANGES_MADE == 'true'"
    assert (
        steps["Create Pull Request"]["if"]
        == "env.CHANGES_MADE == 'true' && env.CORRECTNESS_EXIT_CODE == '0'"
    )
    assert (
        steps["Create Pull Request"]["with"]["labels"]
        == "${{ steps.correctness.outputs.pr_labels }}"
    )

    artifact_step = steps["Upload refactoring artifacts"]
    assert artifact_step["if"] == "always()"
    artifact_paths = artifact_step["with"]["path"].splitlines()
    for name in ("validation_assessment.json", "pr_body.txt", "pr_title.txt"):
        assert "${{ env.TARGET_DIR }}/" + name in artifact_paths


@pytest.mark.parametrize(
    ("test_status", "test_reason", "decision", "exit_code", "expected_labels"),
    [
        (
            "passed",
            "Repository tests passed (pytest exit code 0).",
            "accepted",
            0,
            "refactoring, automated-pr",
        ),
        (
            "skipped",
            "No tests collected; skipping test validation.",
            "accepted",
            0,
            "refactoring, automated-pr, no-tests",
        ),
        (
            "skipped",
            "pytest not found; skipping test validation.",
            "accepted",
            0,
            "refactoring, automated-pr, no-tests",
        ),
        (
            "failed",
            "Repository tests failed (pytest exit code 1).",
            "rejected",
            1,
            "refactoring, automated-pr",
        ),
        (
            "skipped",
            "No tests collected; skipping test validation.",
            "rejected",
            1,
            "refactoring, automated-pr",
        ),
    ],
)
@pytest.mark.parametrize("target_directory", [".", "nested project"])
def test_workflow_passes_assessment_without_rewriting_validation_text(
    tmp_path,
    refactoring_steps,
    test_status,
    test_reason,
    decision,
    exit_code,
    expected_labels,
    target_directory,
):
    steps = {step["name"]: step for step in refactoring_steps}
    target = tmp_path / target_directory
    target.mkdir(exist_ok=True)
    tool_path = tmp_path / ".refactorer-tool"
    scripts_path = tool_path / "scripts"
    shutil.copytree(
        WORKFLOW_PATH.parents[2] / "scripts",
        scripts_path,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for caller_root in {tmp_path, target}:
        caller_scripts = caller_root / "scripts"
        caller_scripts.mkdir()
        (caller_scripts / "__init__.py").write_text(
            "raise AssertionError('Caller scripts package must not be imported')\n"
        )

    # Use the real reporting entry points with a supplied assessment, avoiding
    # repository tests, models, Git operations, and publication.
    (scripts_path / "correctness_filter.py").rename(
        scripts_path / "_correctness_filter.py"
    )
    (scripts_path / "correctness_filter.py").write_text(
        textwrap.dedent(
            """\
            import json
            import os
            from . import _correctness_filter as correctness_filter

            assessment = json.loads(os.environ["ASSESSMENT_JSON"])
            correctness_filter.assess_candidate = lambda: assessment
            raise SystemExit(correctness_filter.main())
            """
        ),
        encoding="utf-8",
    )
    checks = {
        "repository_tests": {"status": test_status, "reason": test_reason},
        "strategy_policy": {
            "status": "not_applicable",
            "reason": "No deletion restrictions apply to this strategy.",
        },
        "deletion_safety": {
            "status": "not_applicable",
            "reason": "No files were deleted.",
        },
        "code_similarity": {
            "status": "passed",
            "reason": "CodeBERTScore F1 meets the threshold.",
            "score": 0.9,
            "threshold": 0.75,
        },
    }
    if decision == "rejected":
        for name in ("strategy_policy", "deletion_safety", "code_similarity"):
            checks[name] = {
                "status": "not_reached",
                "reason": "Stopped after an earlier check failed.",
            }
        if test_status == "skipped":
            checks["strategy_policy"] = {
                "status": "failed",
                "reason": "A required deletion was missing.",
            }
    assessment = {
        "decision": decision,
        "reason": f"Candidate {decision} under the correctness policy.",
        "checks": checks,
    }
    row = {
        "Name": "Cyclic Dependency",
        "File": "package/a.py",
        "Line Number": "1",
        "Description": "Cycle between a and b",
        "Severity": "Critical",
        "Related Participants": "[]",
    }
    with (tmp_path / "filtered_report.csv").open(
        "w", newline="", encoding="utf-8"
    ) as output:
        writer = csv.DictWriter(output, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)
    (target / "proposal.json").write_text(
        json.dumps({"strategy": "ExtractUtility", "explanation": "Share the utility."}),
        encoding="utf-8",
    )
    refactored_files = tmp_path / "pr-refactored-files.json"
    refactored_files.write_text("[]", encoding="utf-8")
    github_env = tmp_path / "github_env"
    github_output = tmp_path / "github_output"
    environment = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "TARGET_DIR": str(target.relative_to(tmp_path)),
        "REFACTORER_TOOL_PATH": str(tool_path),
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(github_output),
        "SMELL_UUID": generate_smell_uuid(row),
        "PR_REFACTORED_FILES_FILE": str(refactored_files),
        "ASSESSMENT_JSON": json.dumps(assessment),
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", steps["Run correctness filter"]["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    outputs = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert outputs == {"CORRECTNESS_EXIT_CODE": str(exit_code)}
    assert github_output.read_text() == f"pr_labels={expected_labels}\n"
    assert json.loads((target / "validation_assessment.json").read_text()) == assessment
    assert not (target / "pr_body.txt").exists()
    assert ("::warning::Correctness filter rejected" in result.stdout) == (exit_code != 0)

    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", steps["Generate PR description"]["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    body = (target / "pr_body.txt").read_text()
    assert f"**Overall decision:** {decision.capitalize()}" in body
    assert f"**Repository tests:** {test_status.capitalize()}. {test_reason}" in body
    deletion_status = checks["deletion_safety"]["status"].replace("_", " ").capitalize()
    assert f"**Deletion safety:** {deletion_status}." in body
    title = (target / "pr_title.txt").read_text()
    assert github_env.read_text() == (
        f"CORRECTNESS_EXIT_CODE={exit_code}\n"
        "PR_TITLE<<END_OF_PR_TITLE_5d8f2a\n"
        f"{title}\n"
        "END_OF_PR_TITLE_5d8f2a\n"
        "PR_BODY<<END_OF_PR_BODY_7c9e3b\n"
        f"{body}\n"
        "END_OF_PR_BODY_7c9e3b\n"
    )
    assert "Tests pass (automated correctness check passed)" not in body
