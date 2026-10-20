from types import SimpleNamespace

from scripts import correctness_filter


# --- Correctness filter: run_repository_tests ---


def test_correctness_filter_reports_passing_tests(tmp_path, monkeypatch, capsys):
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    fake_result = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(
        correctness_filter.subprocess,
        "run",
        lambda *args, **kwargs: fake_result,
    )

    result = correctness_filter.run_repository_tests()

    assert result["status"] == "passed"
    assert "pytest exit code 0" in result["reason"]
    assert "::warning::" not in capsys.readouterr().out
    assert not github_output.exists()


def test_correctness_filter_warns_when_no_tests_are_collected(monkeypatch, capsys):
    fake_result = SimpleNamespace(returncode=5, stdout=b"", stderr=b"")
    monkeypatch.setattr(
        correctness_filter.subprocess,
        "run",
        lambda *args, **kwargs: fake_result,
    )

    result = correctness_filter.run_repository_tests()

    assert result["status"] == "skipped"
    assert result["reason"] == "No tests collected; skipping test validation."
    assert "::warning::No tests collected; skipping test validation." in capsys.readouterr().out


def test_correctness_filter_reports_failed_tests(monkeypatch):
    fake_result = SimpleNamespace(returncode=1, stdout=b"fail", stderr=b"boom")
    monkeypatch.setattr(
        correctness_filter.subprocess,
        "run",
        lambda *args, **kwargs: fake_result,
    )

    result = correctness_filter.run_repository_tests()

    assert result["status"] == "failed"
    assert "pytest exit code 1" in result["reason"]


def test_correctness_filter_gracefully_handles_missing_pytest(monkeypatch, capsys):
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError("pytest")

    monkeypatch.setattr(correctness_filter.subprocess, "run", _raise)

    result = correctness_filter.run_repository_tests()

    assert result["status"] == "skipped"
    assert result["reason"] == "pytest not found; skipping test validation."
    assert "::warning::pytest not found; skipping test validation." in capsys.readouterr().out


# --- Correctness filter: check_deletion_safety ---


def test_check_deletion_safety_passes_with_no_references(tmp_path):
    # Create a file that will be "deleted"
    deleted = tmp_path / "deleted.py"
    deleted.write_text("def old_func(): pass")

    # Create another file that does NOT reference the deleted module
    other = tmp_path / "other.py"
    other.write_text("def unrelated(): pass")

    safe, violations = correctness_filter.check_deletion_safety(
        [str(deleted)], repo_root=str(tmp_path)
    )

    assert safe is True
    assert violations == []


def test_check_deletion_safety_fails_with_import_reference(tmp_path):
    # Create a file that will be "deleted"
    deleted = tmp_path / "deleted.py"
    deleted.write_text("def old_func(): pass")

    # Create another file that references the deleted module
    other = tmp_path / "other.py"
    other.write_text("from deleted import old_func")

    safe, violations = correctness_filter.check_deletion_safety(
        [str(deleted)], repo_root=str(tmp_path)
    )

    assert safe is False
    assert len(violations) == 1
    assert "deleted" in violations[0]


def test_check_deletion_safety_fails_with_import_module_reference(tmp_path):
    # Create a file that will be "deleted"
    deleted = tmp_path / "mymod.py"
    deleted.write_text("x = 1")

    # Create another file that imports the whole module
    other = tmp_path / "consumer.py"
    other.write_text("import mymod\nprint(mymod.x)")

    safe, violations = correctness_filter.check_deletion_safety(
        [str(deleted)], repo_root=str(tmp_path)
    )

    assert safe is False
    assert len(violations) == 1


def test_check_deletion_safety_fails_with_from_pkg_import(tmp_path):
    # Create a package structure
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    # Module to be deleted
    helper = pkg / "helper.py"
    helper.write_text("def assist(): pass")

    # File that imports via 'from pkg import helper'
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from pkg import helper\nhelper.assist()")

    safe, violations = correctness_filter.check_deletion_safety(
        [str(helper)], repo_root=str(tmp_path)
    )

    assert safe is False
    assert len(violations) == 1


# --- Correctness filter: assess_strategy_policy ---


def test_assess_strategy_policy_passes_for_valid_collapse_hierarchy():
    proposal = {"strategy": "CollapseHierarchy", "canonical_module": "base.py"}
    refactored = [
        {"path": "wrapper.py", "operation": "delete", "content": None},
        {"path": "base.py", "operation": "modify", "content": "updated"},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "passed"
    assert result["reason"] == "Refactoring satisfies the applicable strategy rules."


def test_assess_strategy_policy_fails_for_delete_only_collapse_hierarchy():
    proposal = {"strategy": "CollapseHierarchy"}
    refactored = [
        {"path": "wrapper.py", "operation": "delete", "content": None},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "failed"
    assert "modify/create" in result["reason"]


def test_assess_strategy_policy_fails_when_canonical_deleted():
    proposal = {"strategy": "CollapseHierarchy", "canonical_module": "base.py"}
    refactored = [
        {"path": "base.py", "operation": "delete", "content": None},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "failed"
    assert "Canonical module" in result["reason"]


def test_assess_strategy_policy_passes_for_valid_merge_modules():
    proposal = {"strategy": "MergeModules"}
    refactored = [
        {"path": "old.py", "operation": "delete", "content": None},
        {"path": "merged.py", "operation": "modify", "content": "combined"},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "passed"
    assert result["reason"] == "Refactoring satisfies the applicable strategy rules."


def test_assess_strategy_policy_fails_for_delete_only_merge_modules():
    proposal = {"strategy": "MergeModules"}
    refactored = [
        {"path": "a.py", "operation": "delete", "content": None},
        {"path": "b.py", "operation": "delete", "content": None},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "failed"
    assert "modify" in result["reason"]


def test_assess_strategy_policy_passes_for_valid_merge_classes():
    proposal = {"strategy": "MergeClasses"}
    refactored = [
        {"path": "classes.py", "operation": "modify", "content": "merged classes"},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "passed"
    assert result["reason"] == "Refactoring satisfies the applicable strategy rules."


def test_assess_strategy_policy_fails_for_no_modify_merge_classes():
    proposal = {"strategy": "MergeClasses"}
    refactored = [
        {"path": "new.py", "operation": "create", "content": "new content"},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "failed"
    assert "modify" in result["reason"]


def test_assess_strategy_policy_is_inapplicable_for_unknown_strategy():
    # Unknown strategies have no specific rules.
    proposal = {"strategy": "UnknownStrategy"}
    refactored = [
        {"path": "a.py", "operation": "delete", "content": None},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "not_applicable"
    assert result["reason"] == "No strategy rules apply to these operations."


def test_assess_strategy_policy_is_inapplicable_with_empty_proposal():
    proposal = {}
    refactored = [
        {"path": "a.py", "operation": "modify", "content": "code"},
    ]

    result = correctness_filter.assess_strategy_policy(proposal, refactored)

    assert result["status"] == "not_applicable"
    assert result["reason"] == "No strategy rules apply to these operations."
