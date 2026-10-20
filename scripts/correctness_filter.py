#!/usr/bin/env python3
"""Correctness filter: validates refactored code via tests and semantic similarity."""

import json
import logging
import math
import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from .prepare_pr_payload import (
    filter_out_helper_checkout_paths,
    normalize_refactored_file_path,
)


CHECK_IDS = (
    "repository_tests",
    "strategy_policy",
    "deletion_safety",
    "code_similarity",
)


def run_repository_tests() -> dict:
    """Run pytest and distinguish executed tests from permitted skips."""
    try:
        test_env = os.environ.copy()
        if test_env.get("PYTHONPATH"):
            # The workflow's tool import path must not shadow repository packages.
            tool_root = Path(__file__).resolve().parents[1]
            paths = [
                path or os.curdir
                for path in test_env["PYTHONPATH"].split(os.pathsep)
                if not path or Path(path).resolve() != tool_root
            ]
            if paths:
                test_env["PYTHONPATH"] = os.pathsep.join(paths)
            else:
                test_env.pop("PYTHONPATH")
        result = subprocess.run(["pytest"], capture_output=True, env=test_env)
        if result.returncode == 0:
            logging.info("Tests passed successfully.")
            return dict(
                status="passed", reason="Repository tests passed (pytest exit code 0)."
            )
        elif result.returncode == 5:
            message = "No tests collected; skipping test validation."
            logging.warning(message)
            print(f"::warning::{message}")
            return dict(status="skipped", reason=message)
        else:
            logging.error("Tests failed with exit code: %d", result.returncode)
            logging.error(result.stdout.decode())
            logging.error(result.stderr.decode())
            return dict(
                status="failed",
                reason=f"Repository tests failed (pytest exit code {result.returncode}).",
            )
    except FileNotFoundError:
        message = "pytest not found; skipping test validation."
        logging.warning(message)
        print(f"::warning::{message}")
        return dict(status="skipped", reason=message)
    except Exception as exc:
        logging.exception("Cannot run repository tests")
        return dict(status="failed", reason=f"{type(exc).__name__}: {exc}")


def load_json_file(filename: str) -> dict | list:
    """Load JSON file from CWD. Raise if not found (pipeline bug)."""
    with open(filename, "r") as f:
        return json.load(f)


def _similarity_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or PureWindowsPath(raw_path).drive:
        raise ValueError(f"Invalid target-relative path: {raw_path!r}")
    path = normalize_refactored_file_path(raw_path, "")
    if path in {None, ".."}:
        raise ValueError(f"Invalid target-relative path: {raw_path!r}")
    return path


def build_similarity_inputs(
    modules: dict[str, str],
    candidate_sources: dict[str, str],
    participant_paths: set[str],
    changed_operations: dict[str, str],
) -> tuple[str, str]:
    """Combine complete files before and after refactoring, in path order."""
    before, after = [], []
    for path in sorted(participant_paths | changed_operations.keys()):
        operation = changed_operations.get(path)
        if operation == "create":
            if path in modules or path in participant_paths:
                raise ValueError(f"Created file already existed: {path}")
        else:
            before.append(f"# FILE: {path}\n{modules[path]}")
        if operation != "delete":
            after.append(f"# FILE: {path}\n{candidate_sources[path]}")
    return "\n".join(before), "\n".join(after)


def _load_similarity_inputs(
    modules: dict,
    refactored: list,
    smell_details: dict,
    *,
    target_root: Path,
    tool_checkout_repo_path: str = ".refactorer-tool",
) -> tuple[str, str]:
    """Select changed files with Git, then read their current contents.

    Files must not change while this function reads them.
    """
    related = json.loads(smell_details["Related Participants"])
    if not related:
        raise ValueError("Missing smell participants")
    participants = {_similarity_path(p["file_path"]) for p in related}
    if smell_details.get("File") is not None:
        participants.add(_similarity_path(smell_details["File"]))

    root = target_root.resolve()
    repo_path = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root)
    repo_root = Path(os.fsdecode(repo_path).rstrip("\r\n"))
    prefix = root.relative_to(repo_root)
    helper_path = _similarity_path(tool_checkout_repo_path) if tool_checkout_repo_path else ""
    helper_root = repo_root / helper_path if helper_path else None
    operations = {}
    for item in refactored:
        path, operation = _similarity_path(item["path"]), item["operation"]
        if operation not in ("create", "modify", "delete") or (
            operations.get(path, operation) != operation
        ):
            raise ValueError(f"Invalid or conflicting operations for {path}")
        operations[path] = operation

    # Git and the helper setting use paths from the repository root.
    repo_paths = {
        (prefix / path).as_posix(): path
        for path in participants | operations.keys() if path.endswith(".py")
    }
    repo_paths = {
        item["path"]: repo_paths[item["path"]]
        for item in filter_out_helper_checkout_paths(
            [{"path": path} for path in repo_paths], helper_path
        )
    }
    participants &= set(repo_paths.values())
    if not participants:
        raise ValueError("Missing Python smell participants")
    requested = sorted(path for path, target in repo_paths.items() if target in operations)
    status = b""
    if requested:
        status = subprocess.check_output(
            [
                "git", "--literal-pathspecs", "status", "--porcelain=v1",
                "-z", "--untracked-files=all", "--no-renames", "--", *requested,
            ],
            cwd=repo_root,
        )
    changed = {}
    for record in filter(None, status.split(b"\0")):
        path = repo_paths.get(os.fsdecode(record[3:]))
        if path in operations:
            # A create must be a new file, even if Git has already staged it.
            if operations[path] == "create" and record[:2] not in (
                b"??", b" A", b"A ", b"AM", b"AT", b"AD",
            ):
                raise ValueError(f"Created file already existed: {path}")
            changed[path] = operations[path]
    operations = changed

    paths = participants | operations.keys()
    originals = {}
    for raw_path, source in modules.items():
        try:
            path = _similarity_path(raw_path)
        except ValueError:
            continue
        if path not in paths:
            continue
        if not isinstance(source, str) or originals.get(path, source) != source:
            raise ValueError(f"Invalid or conflicting original source for {path}")
        originals[path] = source

    candidates = {}
    for path in sorted(paths):
        file_path = root / path
        resolved = Path(os.path.realpath(file_path))
        resolved.relative_to(root)
        if helper_root is not None and resolved.is_relative_to(helper_root):
            raise ValueError(f"File points inside the helper checkout: {path}")
        if operations.get(path) == "delete":
            if file_path.exists() or file_path.is_symlink():
                raise ValueError(f"Deleted path still exists: {path}")
        else:
            candidates[path] = resolved.read_text(encoding="utf-8")
    return build_similarity_inputs(originals, candidates, participants, operations)


def check_deletion_safety(deleted_paths: list[str], repo_root: str = ".") -> tuple[bool, list[str]]:
    """Check that no remaining .py files reference deleted modules."""
    violations = []

    for deleted_path in deleted_paths:
        # Extract module name from path (e.g., "pkg/mod.py" → "mod")
        path = Path(deleted_path)
        module_name = path.stem

        # Match common import patterns for this module
        patterns = [
            rf"\bimport\s+{re.escape(module_name)}\b",  # import module_name
            rf"\bfrom\s+{re.escape(module_name)}\s+import\b",  # from module_name import ...
            rf"\bfrom\s+[\w.]*\.{re.escape(module_name)}\s+import\b",  # from pkg.module_name import ...
            rf"\bimport\s+[\w.]*\.{re.escape(module_name)}\b",  # import pkg.module_name
            rf"\bfrom\s+[\w.]+\s+import\s+[^#\n]*\b{re.escape(module_name)}\b",  # from pkg import module_name
        ]

        for py_file in Path(repo_root).rglob("*.py"):
            # Skip the deleted file itself
            if py_file.resolve() == Path(deleted_path).resolve():
                continue
            try:
                content = py_file.read_text()
                for pattern in patterns:
                    if re.search(pattern, content):
                        violations.append(f"{py_file}: references {module_name}")
                        break
            except Exception:
                continue

    return len(violations) == 0, violations


def assess_strategy_policy(proposal: dict, refactored: list) -> dict:
    """Validate strategy requirements, distinguishing rules that do not apply."""
    strategy = proposal.get("strategy", "")
    canonical = proposal.get("canonical_module", "")

    ops = {"delete": 0, "modify": 0, "create": 0}
    deleted_paths = []
    for item in refactored:
        op = item.get("operation", "")
        ops[op] = ops.get(op, 0) + 1
        if op == "delete":
            deleted_paths.append(item.get("path", ""))

    # Check canonical not deleted
    if canonical and canonical in deleted_paths:
        return dict(status="failed", reason=f"Canonical module {canonical} was deleted")

    # Strategy-specific rules
    applicable = bool(canonical)
    if strategy == "CollapseHierarchy" and ops["delete"] >= 1:
        applicable = True
        if (ops["modify"] + ops["create"]) == 0:
            return dict(
                status="failed",
                reason="CollapseHierarchy requires at least one modify/create with deletes",
            )

    elif strategy == "MergeModules" and ops["delete"] >= 1:
        applicable = True
        if ops["modify"] == 0:
            return dict(
                status="failed",
                reason="MergeModules requires at least one modify with deletes",
            )

    elif strategy == "MergeClasses":
        applicable = True
        if ops["modify"] == 0:
            return dict(status="failed", reason="MergeClasses requires at least one modify")

    if applicable:
        return dict(
            status="passed", reason="Refactoring satisfies the applicable strategy rules."
        )
    return dict(
        status="not_applicable", reason="No strategy rules apply to these operations."
    )


def check_similarity(candidate: str, reference: str, threshold: float) -> tuple[bool, float]:
    """Compare source using CodeBERTScore. Return (passed, score)."""
    from code_bert_score import score

    # CodeBERTScore returns (P, R, F1, F3)
    _, _, f1, _ = score(cands=[candidate], refs=[reference], lang="python")
    f1_score = f1.item()  # tensor to float

    return f1_score >= threshold, f1_score


def assess_deletion_safety(refactored: list) -> dict:
    """Check deleted modules and retain failures as diagnostic evidence."""
    try:
        deleted_paths = [
            item["path"] for item in refactored if item.get("operation") == "delete"
        ]
        if not deleted_paths:
            return dict(status="not_applicable", reason="No files were deleted.")

        logging.info("Checking deletion safety...")
        safe, violations = check_deletion_safety(deleted_paths)
        if not safe:
            reason = "Remaining references to deleted modules: " + "; ".join(
                sorted(violations)[:5]
            )
            return dict(status="failed", reason=reason)
        return dict(
            status="passed", reason="No remaining references to deleted modules found."
        )
    except Exception as exc:
        logging.exception("Cannot check deletion safety")
        return dict(status="failed", reason=f"{type(exc).__name__}: {exc}")


def assess_code_similarity(modules: dict, refactored: list, proposal: dict) -> dict:
    """Compare complete sources, retaining the threshold if scoring fails."""
    threshold = None
    try:
        before_source, after_source = _load_similarity_inputs(
            modules,
            refactored,
            proposal.get("similarity_context"),
            target_root=Path.cwd(),
            tool_checkout_repo_path=os.environ.get(
                "REFACTORER_TOOL_CHECKOUT_PATH", ".refactorer-tool"
            ),
        )
        if not before_source.strip() or not after_source.strip():
            return dict(
                status="not_applicable", reason="No source on one side to compare."
            )

        # Keep the existing default and environment override unchanged.
        threshold = float(os.environ.get("SIMILARITY_THRESHOLD", "0.75"))
        if not math.isfinite(threshold):
            raise ValueError("Similarity threshold must be a finite number")
        logging.info("Checking semantic similarity (threshold: %.2f)...", threshold)
        passed, score = check_similarity(after_source, before_source, threshold)
        if not math.isfinite(score):
            raise ValueError("Similarity score and threshold must be finite numbers")
        logging.info("CodeBERTScore F1: %.4f", score)
        return dict(
            status="passed" if passed else "failed",
            reason=(
                "CodeBERTScore F1 meets the threshold."
                if passed
                else "CodeBERTScore F1 is below the threshold."
            ),
            score=score,
            threshold=threshold,
        )
    except Exception as exc:
        logging.exception("Cannot check code similarity")
        result = dict(status="failed", reason=f"{type(exc).__name__}: {exc}")
        if threshold is not None and math.isfinite(threshold):
            result["threshold"] = threshold
        return result


def assess_candidate() -> dict:
    """Run each check in order, stopping at the first rejection."""
    checks = {
        name: dict(status="not_reached", reason="Validation has not reached this check.")
        for name in CHECK_IDS
    }

    def reject(reason: str) -> dict:
        logging.error(reason)
        for name, check in checks.items():
            if check["status"] == "not_reached":
                checks[name] = dict(
                    status="not_reached", reason=f"Stopped earlier: {reason}"
                )
        return dict(decision="rejected", reason=reason, checks=checks)

    logging.info("Running pytest...")
    checks["repository_tests"] = run_repository_tests()
    if checks["repository_tests"]["status"] == "failed":
        return reject(checks["repository_tests"]["reason"])

    try:
        logging.info("Loading modules.json and refactored_files.json...")
        modules = load_json_file("modules.json")
        refactored = load_json_file("refactored_files.json")
    except Exception as exc:
        logging.exception("Cannot load validation inputs")
        return reject(
            f"Cannot complete validation input loading: {type(exc).__name__}: {exc}"
        )

    proposal = {}
    try:
        if os.path.exists("proposal.json"):
            proposal = load_json_file("proposal.json")
            checks["strategy_policy"] = assess_strategy_policy(proposal, refactored)
        else:
            checks["strategy_policy"] = dict(
                status="skipped", reason="No proposal was supplied."
            )
    except Exception as exc:
        logging.exception("Cannot check strategy policy")
        checks["strategy_policy"] = dict(
            status="failed", reason=f"{type(exc).__name__}: {exc}"
        )
    if checks["strategy_policy"]["status"] == "failed":
        return reject(f"Strategy policy violation: {checks['strategy_policy']['reason']}")

    checks["deletion_safety"] = assess_deletion_safety(refactored)
    if checks["deletion_safety"]["status"] == "failed":
        return reject(f"Deletion safety failed: {checks['deletion_safety']['reason']}")

    checks["code_similarity"] = assess_code_similarity(modules, refactored, proposal)
    if checks["code_similarity"]["status"] == "failed":
        return reject(checks["code_similarity"]["reason"])

    return dict(
        decision="accepted",
        reason="Candidate accepted under the current correctness policy.",
        checks=checks,
    )


def main() -> int:
    """Write the assessment artifact and return the existing publication decision."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    assessment = assess_candidate()
    Path("validation_assessment.json").write_text(
        json.dumps(assessment, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    # Derive CI labels from the completed assessment used by the PR body.
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        labels = "refactoring, automated-pr"
        if (
            assessment["decision"] == "accepted"
            and assessment["checks"]["repository_tests"]["status"] == "skipped"
        ):
            labels += ", no-tests"
        with open(output_file, "a", encoding="utf-8") as output:
            output.write(f"pr_labels={labels}\n")
    logging.info("Correctness filter decision: %s", assessment["decision"])
    return 0 if assessment["decision"] == "accepted" else 1


if __name__ == "__main__":
    sys.exit(main())
