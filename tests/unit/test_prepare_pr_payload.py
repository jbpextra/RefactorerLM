import json
from pathlib import Path

import pytest

from scripts.prepare_pr_payload import (
    build_filtered_refactored_files_payload,
    filter_out_helper_checkout_paths,
    load_and_normalize_candidate_repo_paths,
    parse_changed_paths_from_status,
)


def write_refactored_files(path: Path, file_ops: list[dict]) -> None:
    path.write_text(json.dumps(file_ops))


def test_load_and_normalize_candidate_repo_paths_keeps_root_target_dir(tmp_path: Path):
    refactored_files_path = tmp_path / "refactored_files.json"
    write_refactored_files(
        refactored_files_path,
        [{"path": "a.py", "operation": "modify", "content": "print('hi')\n"}],
    )

    normalized = load_and_normalize_candidate_repo_paths(refactored_files_path, ".")

    assert normalized == [{"path": "a.py", "operation": "modify", "content": "print('hi')\n"}]


def test_load_and_normalize_candidate_repo_paths_prefixes_nested_target_dir(tmp_path: Path):
    refactored_files_path = tmp_path / "refactored_files.json"
    write_refactored_files(
        refactored_files_path,
        [{"path": "app/models.py", "operation": "modify", "content": "pass\n"}],
    )

    normalized = load_and_normalize_candidate_repo_paths(refactored_files_path, "backend")

    assert normalized == [{"path": "backend/app/models.py", "operation": "modify", "content": "pass\n"}]


def test_filter_out_helper_checkout_paths_drops_tool_checkout_entries():
    refactored_files = [
        {"path": ".refactorer-tool/scripts/prepare_pr_payload.py", "operation": "modify", "content": "x"},
        {"path": "src/app.py", "operation": "modify", "content": "y"},
    ]

    filtered = filter_out_helper_checkout_paths(refactored_files, ".refactorer-tool")

    assert filtered == [{"path": "src/app.py", "operation": "modify", "content": "y"}]


@pytest.mark.parametrize("raw_path", ["/tmp/escape.py", "../escape.py"])
def test_load_and_normalize_candidate_repo_paths_rejects_invalid_paths(
    tmp_path: Path, raw_path: str
):
    refactored_files_path = tmp_path / "refactored_files.json"
    write_refactored_files(
        refactored_files_path,
        [{"path": raw_path, "operation": "modify", "content": "pass\n"}],
    )

    with pytest.raises(ValueError, match="escapes repository root"):
        load_and_normalize_candidate_repo_paths(refactored_files_path, ".")


def test_parse_changed_paths_from_status_keeps_rename_destination():
    changed_paths = parse_changed_paths_from_status("R  old.py -> new.py\n", ".refactorer-tool")

    assert changed_paths == ["new.py"]


def test_parse_changed_paths_from_status_keeps_deleted_paths():
    changed_paths = parse_changed_paths_from_status("D  deleted.py\n", ".refactorer-tool")

    assert changed_paths == ["deleted.py"]


def test_build_filtered_refactored_files_payload_keeps_only_changed_paths():
    refactored_files = [
        {"path": "a.py", "operation": "modify", "content": "a\n"},
        {"path": "b.py", "operation": "modify", "content": "b\n"},
    ]

    filtered = build_filtered_refactored_files_payload(refactored_files, ["b.py"])

    assert filtered == [{"path": "b.py", "operation": "modify", "content": "b\n"}]
