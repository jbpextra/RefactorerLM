#!/usr/bin/env python3
import argparse
import sys
import os
import json
from pathlib import Path, PureWindowsPath

from .llm_clients import execute_refactor

DEBUG_LOG = "debug.log"

# Define log levels
DEBUG = "DEBUG"
INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"

# Map levels to severity integers
LOG_LEVELS = {
    DEBUG: 0,
    INFO: 1,
    WARNING: 2,
    ERROR: 3
}

# Minimum level to print to standard output
MIN_PRINT_LEVEL_STDOUT = LOG_LEVELS[INFO]


def validate_operation_paths(
    file_operations: list[dict],
    analysis_directory: Path,
    tool_directory: Path | None = None,
) -> list[dict]:
    """Validate every path before mutation; return operations with normalized paths.

    Model paths are relative to the analysis directory. Check every existing
    prefix after resolving symlinks, including parents of files yet to be
    created, so aliases cannot enter the helper checkout or leave the target.
    Keep relative paths in the artifact and preserve symlink deletion semantics.
    """
    analysis_directory = analysis_directory.resolve()
    tool_directory = tool_directory.resolve() if tool_directory is not None else None
    validated_operations = []
    for file_op in file_operations:
        raw_path = file_op["path"]
        if not isinstance(raw_path, str) or not raw_path or "\0" in raw_path:
            raise ValueError(f"Invalid refactored file path: {raw_path!r}")

        # Match the separator handling used by prepare_pr_payload on all hosts.
        relative_path = Path(raw_path.replace("\\", "/"))
        if relative_path.is_absolute() or PureWindowsPath(raw_path).drive:
            raise ValueError(f"Refactored file path must be relative: {raw_path!r}")
        if not relative_path.parts or ".." in relative_path.parts:
            raise ValueError(
                f"Invalid parent-relative refactored file path: {raw_path!r}"
            )

        candidate = analysis_directory
        for part in relative_path.parts:
            candidate = candidate / part
            resolved = candidate.resolve()
            if not resolved.is_relative_to(analysis_directory):
                raise ValueError(
                    f"Refactored file path escapes analysis directory: {raw_path!r}"
                )
            if tool_directory is not None and resolved.is_relative_to(tool_directory):
                raise ValueError(
                    f"Refactored file path enters Refactorer tool checkout: {raw_path!r}"
                )

        if resolved == analysis_directory:
            raise ValueError(
                f"Refactored file path points to analysis directory: {raw_path!r}"
            )
        validated_operations.append({**file_op, "path": relative_path.as_posix()})

    return validated_operations


def log(message, level=INFO):
    """Logs a message with a specified level.
    Prints to stdout only if the level is INFO or higher.
    Always writes to log file regardless of level.
    """
    with open(DEBUG_LOG, "a") as f:
        f.write(f"[{level}] {message}\n")

    # Print to standard output only if level is INFO or above
    if LOG_LEVELS.get(level, LOG_LEVELS[INFO]) >= MIN_PRINT_LEVEL_STDOUT:
        print(f"[{level}] {message}")

def main():
    parser = argparse.ArgumentParser(description="Apply refactoring plan proposal for a specific smell")
    parser.add_argument("--smell-uuid", required=True, help="UUID of the smell to refactor")
    parser.add_argument("--proposal-file", required=True, help="Path to the refactor plan proposal JSON file")
    parser.add_argument("--modules-file", required=False, help="Path to the modules JSON file")

    args = parser.parse_args()
    plan = json.load(open(args.proposal_file, "r"))
    modules = json.load(open(args.modules_file, "r"))

    # Ensure the OPENAI_API_KEY environment variable is set.
    if "OPENAI_API_KEY" not in os.environ:
        log("OPENAI_API_KEY environment variable not set.")
        sys.exit(1)

    # Extract importers metadata (with defaults)
    importers_map = plan.get("importers_by_participant", {})
    # The workflow changes to its resolved TARGET_DIR and exports the absolute
    # helper checkout path. Never infer the helper location from the target.
    analysis_directory = Path.cwd().resolve()
    tool_path = os.environ.get("REFACTORER_TOOL_PATH")
    tool_directory = Path(tool_path) if tool_path else None

    try:
        # Execute the refactoring plan
        log(f"Executing refactoring plan...", INFO)
        refactored_files = execute_refactor(
            plan,
            modules,
            importers_map,
        )
        refactored_files = validate_operation_paths(
            refactored_files, analysis_directory, tool_directory
        )

        # Save artifacts
        with open("refactored_files.json", "w") as f:
            json.dump(refactored_files, f, indent=2)

        if not refactored_files:
            log(f"Execution returned no files for smell", ERROR)
            sys.exit(1)

        # Preserve originals outside the planner's context before any file changes.
        for file_op in refactored_files:
            path = file_op["path"]
            file_path = analysis_directory / path
            if (
                file_op["operation"] in {"modify", "delete"}
                and path not in modules
                and file_path.is_file()
            ):
                modules[path] = file_path.read_text(encoding="utf-8")
        with open(args.modules_file, "w", encoding="utf-8") as f:
            json.dump(modules, f, indent=2, sort_keys=True)

        # Apply the refactored files to the codebase
        log(f"Applying refactored files...", INFO)

        # Write the refactored files back to disk
        for file_op in refactored_files:
            file_path = analysis_directory / file_op["path"]
            content = file_op["content"]
            operation = file_op["operation"]

            log(f"Processing {operation} for {file_path}", DEBUG)

            if operation != "delete":
                file_path.parent.mkdir(parents=True, exist_ok=True)

            # Process file according to operation
            if operation == "delete":
                if os.path.exists(file_path):
                    file_path.unlink()
                    log(f"Deleted file: {file_path}", INFO)
                else:
                    log(f"Delete skipped; file missing: {file_path}", WARNING)
            elif operation in {"create", "modify"}:
                with file_path.open("w") as f:
                    f.write(content or "")
                log(f"Applied {operation}: {file_path}", INFO)
            else:
                log(f"Unsupported operation '{operation}' for {file_path}", WARNING)

    except Exception as e:
        log(f"Error refactoring smell: {str(e)}", ERROR)
        sys.exit(1)

if __name__ == "__main__":
    main()
