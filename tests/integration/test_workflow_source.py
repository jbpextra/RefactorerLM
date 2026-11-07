"""Private runs select private helpers and freeze their revision across jobs."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/refactorer.yml"


@pytest.fixture
def workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step(workflow, step_id):
    return next(
        step
        for step in workflow["jobs"]["detect-smells"]["steps"]
        if step.get("id") == step_id
    )


def _run_step(step, tmp_path, **environment):
    output = tmp_path / "outputs.txt"
    output.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output), **environment},
        capture_output=True,
        text=True,
    )
    outputs = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, outputs


@pytest.mark.parametrize(
    ("trigger", "repository", "ref", "expected_repository", "expected_ref"),
    [
        ("workflow_call", None, None, "jbpextra/RefactorerLM", "main"),
        ("workflow_call", "", "", "jbpextra/RefactorerLM", "main"),
        (
            "workflow_call",
            "jbpextra/Refactorer",
            "a" * 40,
            "jbpextra/Refactorer",
            "a" * 40,
        ),
        ("workflow_dispatch", None, None, "jbpextra/Refactorer", "b" * 40),
        (
            "workflow_dispatch",
            "self",
            "feature/candidate",
            "jbpextra/Refactorer",
            "feature/candidate",
        ),
        (
            "workflow_dispatch",
            "jbpextra/RefactorerLM",
            "",
            "jbpextra/RefactorerLM",
            "main",
        ),
        (
            "workflow_dispatch",
            "jbpextra/Refactorer",
            "release-candidate",
            "jbpextra/Refactorer",
            "release-candidate",
        ),
    ],
)
def test_source_resolution_uses_trigger_defaults_without_caller_event_guessing(
    workflow, tmp_path, trigger, repository, ref, expected_repository, expected_ref
):
    settings = workflow["on"][trigger]["inputs"]
    result, outputs = _run_step(
        _step(workflow, "resolve-source"),
        tmp_path,
        INPUT_REPOSITORY=(
            settings["refactorer_repository"]["default"]
            if repository is None
            else repository
        ),
        INPUT_REF=settings["refactorer_ref"]["default"] if ref is None else ref,
        CALLER_REPOSITORY="jbpextra/Refactorer",
        WORKFLOW_SHA="b" * 40,
        # A reusable workflow can itself be invoked by a manual caller. This
        # event must never make its public default switch to the caller repo.
        GITHUB_EVENT_NAME="workflow_dispatch",
    )
    assert result.returncode == 0, result.stderr
    assert outputs == {"repository": expected_repository, "ref": expected_ref}


@pytest.mark.parametrize(
    ("repository", "ref"),
    [
        ("owner/repo\nref=unexpected", "main"),
        ("not-a-repository", "main"),
        ("owner/repo", "main\nrepository=unexpected/repo"),
        ("owner/repo", "main\rnext"),
    ],
)
def test_invalid_source_cannot_inject_checkout_outputs(workflow, tmp_path, repository, ref):
    result, outputs = _run_step(
        _step(workflow, "resolve-source"),
        tmp_path,
        INPUT_REPOSITORY=repository,
        INPUT_REF=ref,
        CALLER_REPOSITORY="jbpextra/Refactorer",
        WORKFLOW_SHA="b" * 40,
    )
    assert result.returncode != 0
    assert "Invalid Refactorer repository or ref" in result.stdout
    assert outputs == {}


def _git(directory, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(directory), *arguments], text=True, stderr=subprocess.PIPE
    ).strip()


def _commit_helper(repository, identity):
    scripts = repository / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "__init__.py").touch()
    (scripts / "identify.py").write_text(f"print({identity!r})\n", encoding="utf-8")
    _git(repository, "add", "scripts")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        identity,
    )
    return _git(repository, "rev-parse", "HEAD")


def _clone(repository, destination, ref):
    subprocess.run(
        ["git", "clone", "--quiet", str(repository), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(destination, "checkout", "--quiet", ref)


def _run_helper(checkout, caller):
    return subprocess.check_output(
        [sys.executable, "-P", "-m", "scripts.identify"],
        cwd=caller,
        env={**os.environ, "PYTHONPATH": str(checkout)},
        text=True,
    ).strip()


def test_private_helper_commit_is_reused_after_private_main_advances(workflow, tmp_path):
    private = tmp_path / "private"
    public = tmp_path / "public"
    for repository in (private, public):
        repository.mkdir()
        _git(repository, "init", "--initial-branch=main")
    private_sha = _commit_helper(private, "private candidate")
    _commit_helper(public, "public release")
    result, selected = _run_step(
        _step(workflow, "resolve-source"),
        tmp_path,
        INPUT_REPOSITORY="jbpextra/Refactorer",
        INPUT_REF="main",
        CALLER_REPOSITORY="customer/python-project",
        WORKFLOW_SHA="c" * 40,
    )
    assert result.returncode == 0, result.stderr
    repositories = {"jbpextra/Refactorer": private, "jbpextra/RefactorerLM": public}
    detector_checkout = tmp_path / "detector-tool"
    _clone(repositories[selected["repository"]], detector_checkout, selected["ref"])
    result, frozen = _run_step(
        _step(workflow, "freeze-source"),
        tmp_path,
        REFACTORER_TOOL_PATH=str(detector_checkout),
    )
    assert result.returncode == 0, result.stderr
    assert frozen == {"sha": private_sha}
    assert _run_helper(detector_checkout, tmp_path) == "private candidate"

    _commit_helper(private, "later private change")
    refactoring_checkout = tmp_path / "refactoring-tool"
    _clone(repositories[selected["repository"]], refactoring_checkout, frozen["sha"])
    assert _run_helper(refactoring_checkout, tmp_path) == "private candidate"
    assert _git(refactoring_checkout, "rev-parse", "HEAD") == private_sha


def test_every_later_tool_checkout_uses_the_frozen_source(workflow):
    detection = workflow["jobs"]["detect-smells"]
    steps = detection["steps"]
    first, second = [
        step
        for step in steps
        if step.get("uses") == "actions/checkout@v4"
        and "repository" in step.get("with", {})
    ]
    assert first["with"]["repository"] == "${{ steps.resolve-source.outputs.repository }}"
    assert first["with"]["ref"] == "${{ steps.resolve-source.outputs.ref }}"
    assert steps.index(first) < steps.index(_step(workflow, "freeze-source"))
    assert steps.index(_step(workflow, "freeze-source")) < steps.index(second)
    assert second["with"]["repository"] == first["with"]["repository"]
    assert second["with"]["ref"] == "${{ steps.freeze-source.outputs.sha }}"
    assert detection["outputs"]["refactorer-repository"] == first["with"]["repository"]
    assert detection["outputs"]["refactorer-sha"] == second["with"]["ref"]

    attempts = workflow["jobs"]["refactor-smells"]["steps"]
    final = next(step for step in attempts if "repository" in step.get("with", {}))
    assert final["with"]["repository"] == (
        "${{ needs.detect-smells.outputs.refactorer-repository }}"
    )
    assert final["with"]["ref"] == "${{ needs.detect-smells.outputs.refactorer-sha }}"
    for checkout in (first, second, final):
        assert checkout["with"]["token"] == (
            "${{ secrets.REFACTORER_REPOSITORY_TOKEN || github.token }}"
        )
