#!/usr/bin/env python3
"""Prepare pull request path payloads from refactored_files.json."""

import argparse
import json
import posixpath
import subprocess
from pathlib import Path
from typing import Any


def normalize_target_dir_prefix(target_dir: str) -> str:
    """Return the repo-root prefix for target-directory-relative file paths."""
    if target_dir in ("", "."):
        return ""
    return target_dir.replace("\\", "/").strip("/")


def normalize_refactored_file_path(raw_path: Any, prefix: str) -> str | None:
    """Normalize a target-directory-relative path into a repo-root-relative path."""
    normalized_path = posixpath.normpath(str(raw_path).replace("\\", "/"))

    if normalized_path in {"", "."}:
        return None

    if (
        posixpath.isabs(normalized_path)
        or normalized_path.startswith("../")
        or "/../" in normalized_path
    ):
        raise ValueError(f"Refactored file path escapes repository root: {raw_path}")

    repo_path = normalized_path if not prefix else posixpath.normpath(f"{prefix}/{normalized_path}")
    if repo_path == ".":
        return None
    return repo_path


def load_refactored_files(refactored_files_path: Path) -> list[dict[str, Any]]:
    """Load refactored file operations from disk."""
    with open(refactored_files_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_and_normalize_candidate_repo_paths(
    refactored_files_path: Path, target_dir: str
) -> list[dict[str, Any]]:
    """Load refactored files and rewrite their paths relative to the repository root."""
    prefix = normalize_target_dir_prefix(target_dir)
    normalized_refactored_files = []

    for file_op in load_refactored_files(refactored_files_path):
        repo_path = normalize_refactored_file_path(file_op["path"], prefix)
        if repo_path is None:
            continue

        normalized_file_op = dict(file_op)
        normalized_file_op["path"] = repo_path
        normalized_refactored_files.append(normalized_file_op)

    return normalized_refactored_files


def normalize_tool_checkout_path(tool_checkout_path: str) -> str:
    """Normalize the helper checkout path for repo-root comparisons."""
    return tool_checkout_path.replace("\\", "/").strip("/")


def filter_out_helper_checkout_paths(
    refactored_files: list[dict[str, Any]], tool_checkout_path: str
) -> list[dict[str, Any]]:
    """Exclude helper checkout files from the PR payload."""
    normalized_tool_checkout_path = normalize_tool_checkout_path(tool_checkout_path)
    if not normalized_tool_checkout_path:
        return list(refactored_files)

    filtered_refactored_files = []
    for file_op in refactored_files:
        repo_path = file_op["path"]
        if repo_path == normalized_tool_checkout_path or repo_path.startswith(
            f"{normalized_tool_checkout_path}/"
        ):
            continue
        filtered_refactored_files.append(file_op)
    return filtered_refactored_files


def parse_changed_paths_from_status(
    status_output: str, tool_checkout_path: str
) -> list[str]:
    """Extract changed repo paths from git status porcelain output."""
    normalized_tool_checkout_path = normalize_tool_checkout_path(tool_checkout_path)
    changed_paths = []

    for line in status_output.splitlines():
        if not line:
            continue

        path_text = line[3:].strip()
        if not path_text:
            continue

        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]

        normalized_status_path = path_text.replace("\\", "/")
        if normalized_tool_checkout_path and (
            normalized_status_path == normalized_tool_checkout_path
            or normalized_status_path.startswith(f"{normalized_tool_checkout_path}/")
        ):
            continue

        changed_paths.append(normalized_status_path)

    return sorted(dict.fromkeys(changed_paths))


def resolve_changed_paths_from_git_status(
    candidate_paths: list[str], tool_checkout_path: str
) -> list[str]:
    """Use git status as the source of truth for which candidate paths changed."""
    if not candidate_paths:
        return []

    result = subprocess.run(
        ["git", "status", "--porcelain", "--", *candidate_paths],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_changed_paths_from_status(result.stdout, tool_checkout_path)


def build_filtered_refactored_files_payload(
    refactored_files: list[dict[str, Any]], changed_paths: list[str]
) -> list[dict[str, Any]]:
    """Keep only refactored file operations that correspond to changed repo paths."""
    changed_path_set = set(changed_paths)
    return [file_op for file_op in refactored_files if file_op["path"] in changed_path_set]


def write_outputs(
    paths_output: Path,
    status_output: Path,
    filtered_refactored_files_output: Path,
    changed_paths: list[str],
    filtered_refactored_files: list[dict[str, Any]],
) -> None:
    """Write workflow temp files consumed by later shell steps."""
    with open(paths_output, "w", encoding="utf-8") as handle:
        for path in changed_paths:
            handle.write(f"{path}\n")

    with open(status_output, "w", encoding="utf-8") as handle:
        handle.write("true" if changed_paths else "false")

    with open(filtered_refactored_files_output, "w", encoding="utf-8") as handle:
        json.dump(filtered_refactored_files, handle, indent=2)
        handle.write("\n")


def prepare_pr_payload(
    refactored_files_path: Path,
    target_dir: str,
    tool_checkout_path: str,
    paths_output: Path,
    status_output: Path,
    filtered_refactored_files_output: Path,
) -> None:
    """Prepare PR paths, change status, and filtered refactored_files payload."""
    normalized_refactored_files = load_and_normalize_candidate_repo_paths(
        refactored_files_path=refactored_files_path,
        target_dir=target_dir,
    )
    caller_repo_refactored_files = filter_out_helper_checkout_paths(
        normalized_refactored_files,
        tool_checkout_path=tool_checkout_path,
    )
    candidate_paths = sorted({file_op["path"] for file_op in caller_repo_refactored_files})
    changed_paths = resolve_changed_paths_from_git_status(
        candidate_paths=candidate_paths,
        tool_checkout_path=tool_checkout_path,
    )
    filtered_refactored_files = build_filtered_refactored_files_payload(
        caller_repo_refactored_files,
        changed_paths=changed_paths,
    )
    write_outputs(
        paths_output=paths_output,
        status_output=status_output,
        filtered_refactored_files_output=filtered_refactored_files_output,
        changed_paths=changed_paths,
        filtered_refactored_files=filtered_refactored_files,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare pull request payload files from refactored_files.json"
    )
    parser.add_argument(
        "--refactored-files",
        required=True,
        help="Path to refactored_files.json",
    )
    parser.add_argument(
        "--target-dir",
        required=True,
        help="Target directory used when apply_refactoring.py wrote file paths",
    )
    parser.add_argument(
        "--tool-checkout-path",
        required=True,
        help="Workspace-relative path of the helper Refactorer checkout",
    )
    parser.add_argument(
        "--paths-output",
        required=True,
        help="Path to write the newline-delimited PR paths file",
    )
    parser.add_argument(
        "--status-output",
        required=True,
        help="Path to write the true/false changes-made status file",
    )
    parser.add_argument(
        "--filtered-refactored-files-output",
        required=True,
        help="Path to write the filtered refactored_files.json payload",
    )
    args = parser.parse_args()

    prepare_pr_payload(
        refactored_files_path=Path(args.refactored_files),
        target_dir=args.target_dir,
        tool_checkout_path=args.tool_checkout_path,
        paths_output=Path(args.paths_output),
        status_output=Path(args.status_output),
        filtered_refactored_files_output=Path(args.filtered_refactored_files_output),
    )


if __name__ == "__main__":
    main()
