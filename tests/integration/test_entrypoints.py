"""Package imports and module entrypoints work outside the tool checkout."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import correctness_filter


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "imports",
    [
        "import scripts.detector_report; import scripts.smell_identity",
        "import scripts.smell_identity; import scripts.detector_report",
    ],
)
def test_fresh_package_imports_do_not_require_test_aliases(tmp_path, imports):
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            f"{imports}; import scripts.correctness_filter",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "selection_config",
        "filter_smells",
        "generate_proposal",
        "generate_pr_description",
        "generate_summary",
        "apply_refactoring",
        "prepare_pr_payload",
    ],
)
def test_stage_module_help_from_another_directory(tmp_path, module):
    result = subprocess.run(
        [sys.executable, "-P", "-m", f"scripts.{module}", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize(
    "remaining_paths", [[], [""], ["dependencies"], ["", "dependencies"]]
)
def test_repository_tests_import_caller_package(tmp_path, monkeypatch, remaining_paths):
    caller_package = tmp_path / "scripts"
    caller_package.mkdir()
    (caller_package / "__init__.py").write_text("CALLER = True\n")
    pythonpath = os.pathsep.join([str(ROOT), *remaining_paths])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    run = subprocess.run

    def run_with_caller_import(command, **kwargs):
        assert command == ["pytest"]
        assert kwargs["env"].get("PYTHONPATH") == (
            os.pathsep.join(path or os.curdir for path in remaining_paths)
            if remaining_paths
            else None
        )
        # Reproduce pytest's append import mode without installed packages.
        return run(
            [
                sys.executable,
                "-P",
                "-S",
                "-c",
                "import os, sys; sys.path.append(os.getcwd()); "
                "from scripts import CALLER; assert CALLER",
            ],
            **kwargs,
        )

    monkeypatch.setattr(correctness_filter.subprocess, "run", run_with_caller_import)

    assert correctness_filter.run_repository_tests()["status"] == "passed"
    assert os.environ["PYTHONPATH"] == pythonpath
