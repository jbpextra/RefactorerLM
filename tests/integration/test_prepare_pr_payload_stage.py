"""Prepare publication artifacts from candidate operations and Git status."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prepare_pr_payload import prepare_pr_payload


def write_refactored_files(path: Path, file_ops: list[dict]) -> None:
    path.write_text(json.dumps(file_ops))


def test_prepare_pr_payload_marks_no_changes_when_git_status_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    refactored_files_path = tmp_path / "refactored_files.json"
    paths_output = tmp_path / "pr-paths.txt"
    status_output = tmp_path / "pr-status.txt"
    filtered_output = tmp_path / "pr-refactored-files.json"
    write_refactored_files(
        refactored_files_path,
        [{"path": "a.py", "operation": "modify", "content": "print('hi')\n"}],
    )

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("scripts.prepare_pr_payload.subprocess.run", fake_run)

    prepare_pr_payload(
        refactored_files_path=refactored_files_path,
        target_dir=".",
        tool_checkout_path=".refactorer-tool",
        paths_output=paths_output,
        status_output=status_output,
        filtered_refactored_files_output=filtered_output,
    )

    assert paths_output.read_text() == ""
    assert status_output.read_text() == "false"
    assert json.loads(filtered_output.read_text()) == []


def test_prepare_pr_payload_filters_to_changed_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    refactored_files_path = tmp_path / "refactored_files.json"
    paths_output = tmp_path / "pr-paths.txt"
    status_output = tmp_path / "pr-status.txt"
    filtered_output = tmp_path / "pr-refactored-files.json"
    write_refactored_files(
        refactored_files_path,
        [
            {"path": "app/models.py", "operation": "modify", "content": "models\n"},
            {"path": "app/views.py", "operation": "modify", "content": "views\n"},
        ],
    )

    def fake_run(cmd, **kwargs):
        assert cmd == [
            "git",
            "status",
            "--porcelain",
            "--",
            "backend/app/models.py",
            "backend/app/views.py",
        ]
        return SimpleNamespace(stdout=" M backend/app/views.py\n")

    monkeypatch.setattr("scripts.prepare_pr_payload.subprocess.run", fake_run)

    prepare_pr_payload(
        refactored_files_path=refactored_files_path,
        target_dir="backend",
        tool_checkout_path=".refactorer-tool",
        paths_output=paths_output,
        status_output=status_output,
        filtered_refactored_files_output=filtered_output,
    )

    assert paths_output.read_text() == "backend/app/views.py\n"
    assert status_output.read_text() == "true"
    assert json.loads(filtered_output.read_text()) == [
        {"path": "backend/app/views.py", "operation": "modify", "content": "views\n"}
    ]
