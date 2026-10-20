"""Workflow boundaries must fail before creating attempts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/refactorer.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _step(step_id):
    return next(
        step
        for step in _workflow()["jobs"]["detect-smells"]["steps"]
        if step.get("id") == step_id
    )


def _environment(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_dir = tmp_path / ".refactorer-tool"
    (tool_dir / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/__init__.py", tool_dir / "scripts/__init__.py")
    return {
        **os.environ,
        "PATH": os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.environ["PATH"]]
        ),
        "CONFIG_FILE": str(tmp_path / "config.yml"),
        "REFACTORER_TOOL_PATH": str(tool_dir),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs.txt"),
        "GITHUB_ENV": str(tmp_path / "env.txt"),
    }


def _run_shell(script, tmp_path, env):
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "contents", ["smell_filter: [\n", "max_proposal_limit: !!int nonsense\n"]
)
def test_invalid_yaml_with_explicit_target_stops_before_detector(tmp_path, contents):
    env = _environment(tmp_path)
    env["TARGET_DIRECTORY_INPUT"] = "."
    Path(env["CONFIG_FILE"]).write_text(contents)
    validator = Path(env["REFACTORER_TOOL_PATH"]) / "scripts/selection_config.py"
    shutil.copyfile(ROOT / "scripts/selection_config.py", validator)
    detector = tmp_path / "bin/analyze_code_quality"
    detector.write_text("#!/bin/sh\ntouch detector-called report.csv\n")
    detector.chmod(0o755)
    detector_script = _step("smell-detector")["run"].replace(
        "${{ env.REFACTORER_TOOL_CHECKOUT_PATH }}", ".refactorer-tool"
    )
    result = _run_shell(
        _step("validate-configuration")["run"] + "\n" + detector_script,
        tmp_path,
        env,
    )
    assert result.returncode == 1
    assert "Configuration validation failed" in result.stderr
    assert env["CONFIG_FILE"] in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "detector-called").exists()
    assert not (tmp_path / "report.csv").exists()
    assert not (tmp_path / "smells_to_process.json").exists()
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@pytest.mark.parametrize(
    "explicit_target, expected", [("", "."), ("nested", "nested")]
)
def test_workflow_validates_defaults_and_resolves_target(tmp_path, explicit_target, expected):
    env = _environment(tmp_path)
    env["TARGET_DIRECTORY_INPUT"] = explicit_target
    Path(env["CONFIG_FILE"]).write_text("{}\n")
    validator = Path(env["REFACTORER_TOOL_PATH"]) / "scripts/selection_config.py"
    shutil.copyfile(ROOT / "scripts/selection_config.py", validator)
    caller_scripts = tmp_path / "scripts"
    caller_scripts.mkdir()
    (caller_scripts / "__init__.py").write_text(
        "raise AssertionError('Caller scripts package must not be imported')\n"
    )
    result = _run_shell(_step("validate-configuration")["run"], tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert Path(env["GITHUB_OUTPUT"]).read_text() == f"target-directory={expected}\n"
    assert not validator.parent.exists(), "tool code and caches must not be scanned"


@pytest.mark.parametrize("report_kind", ["missing", "zero-byte", "header-only"])
def test_workflow_requires_detector_report_but_accepts_header_only(tmp_path, report_kind):
    env = _environment(tmp_path)
    env["TARGET_DIR"] = "."
    (Path(env["REFACTORER_TOOL_PATH"]) / "code_quality_config.yaml").write_text("{}\n")
    detector = tmp_path / "bin/analyze_code_quality"
    detector_source = (
        f"#!{sys.executable}\nimport pathlib, sys\n"
        "pathlib.Path('detector-called').touch()\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
    )
    if report_kind != "missing":
        contents = ""
        if report_kind == "header-only":
            contents = (
                (ROOT / "tests/fixtures/reports/detector_report.csv")
                .read_text()
                .splitlines()[0]
                + "\n"
            )
        detector_source += f"output.write_text({contents!r})\n"
    detector.write_text(detector_source)
    detector.chmod(0o755)
    detector_script = _step("smell-detector")["run"].replace(
        "${{ env.REFACTORER_TOOL_CHECKOUT_PATH }}", ".refactorer-tool"
    )
    result = _run_shell(detector_script + "\ntouch selection-reached\n", tmp_path, env)
    assert (tmp_path / "detector-called").exists()
    if report_kind == "header-only":
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "selection-reached").exists()
        assert "smells-detected=true" in Path(env["GITHUB_OUTPUT"]).read_text()
    else:
        assert result.returncode != 0
        assert "::error::Smell detector did not produce a nonempty CSV report" in result.stdout
        assert not (tmp_path / "selection-reached").exists()
        assert not Path(env["GITHUB_OUTPUT"]).exists()
        assert not (tmp_path / "smells_to_process.json").exists()


@pytest.mark.parametrize(
    "branch_output, exit_code",
    [
        ("", 0),
        ("  origin/main\n", 0),
        ("  origin/main\n  origin/refactor/previous-attempt\n", 0),
        ("  origin/refactor/partial-result\n", 42),
    ],
)
def test_branch_listing_failure_is_not_an_empty_attempt_history(
    tmp_path, branch_output, exit_code
):
    env = _environment(tmp_path)
    git = tmp_path / "bin/git"
    git.write_text(
        f"#!{sys.executable}\nimport fnmatch, json, pathlib, sys\n"
        "pathlib.Path('git-arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"for branch in {branch_output!r}.splitlines():\n"
        "    if fnmatch.fnmatchcase(branch.strip(), sys.argv[-1]):\n"
        "        print(branch)\n"
        f"sys.exit({exit_code})\n"
    )
    git.chmod(0o755)
    selector = Path(env["REFACTORER_TOOL_PATH"]) / "scripts/filter_smells.py"
    selector.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path('selector-arguments.json').write_text(json.dumps(sys.argv))\n"
        "pathlib.Path('smells_detected.txt').write_text('false')\n"
        "pathlib.Path('smells_to_process.json').write_text('[]')\n"
    )
    result = _run_shell(_step("filter-smells")["run"], tmp_path, env)
    assert json.loads((tmp_path / "git-arguments.json").read_text()) == [
        "branch",
        "-r",
        "--list",
        "origin/refactor/*",
    ]
    arguments_file = tmp_path / "selector-arguments.json"
    if exit_code:
        assert result.returncode != 0
        assert "Cannot read previous-attempt state" in result.stdout
        assert not arguments_file.exists()
        assert not (tmp_path / "smells_to_process.json").exists()
        assert not Path(env["GITHUB_OUTPUT"]).exists()
    else:
        assert result.returncode == 0, result.stderr
        arguments = json.loads(arguments_file.read_text())
        expected = "\n".join(
            line for line in branch_output.splitlines() if "origin/refactor/" in line
        )
        assert arguments[arguments.index("--existing-branches") + 1] == expected
        assert Path(env["GITHUB_OUTPUT"]).read_text() == (
            "smells-detected=false\nsmell-uuids=[]\n"
        )


def test_attempt_jobs_require_successful_detection_and_validation_runs_first():
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["refactor-smells"]["if"] == (
        "needs.detect-smells.result == 'success' && "
        "needs.detect-smells.outputs.smells-detected == 'true'"
    )
    steps = jobs["detect-smells"]["steps"]
    ids = [step.get("id") for step in steps]
    assert ids.index("validate-configuration") < ids.index("smell-detector")
    assert _step("smell-detector")["env"]["TARGET_DIR"] == (
        "${{ steps.validate-configuration.outputs.target-directory }}"
    )
    sparse_checkout = next(
        step["with"]["sparse-checkout"]
        for step in steps
        if "sparse-checkout" in step.get("with", {})
    )
    assert "scripts/__init__.py" in sparse_checkout.splitlines()
    assert "scripts/selection_config.py" in sparse_checkout.splitlines()
