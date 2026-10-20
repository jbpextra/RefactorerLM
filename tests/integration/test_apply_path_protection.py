import json
import sys

import pytest

from scripts import apply_refactoring


@pytest.fixture
def run_refactoring(monkeypatch):
    def run(target, operations, helper=None):
        monkeypatch.chdir(target)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        if helper is None:
            monkeypatch.delenv("REFACTORER_TOOL_PATH", raising=False)
        else:
            monkeypatch.setenv("REFACTORER_TOOL_PATH", str(helper))
        monkeypatch.setattr(
            apply_refactoring,
            "execute_refactor",
            lambda plan, modules, importers_map: operations,
        )
        (target / "proposal.json").write_text(json.dumps({"actions": []}))
        (target / "modules.json").write_text(json.dumps({}))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "apply_refactoring.py",
                "--smell-uuid",
                "test-smell",
                "--proposal-file",
                "proposal.json",
                "--modules-file",
                "modules.json",
            ],
        )
        apply_refactoring.main()

    return run


def assert_batch_rejected(run_refactoring, target, invalid_operation, helper=None):
    """An invalid final operation must leave all preceding operations unapplied."""
    (target / "modified.py").write_text("original\n")
    (target / "deleted.py").write_text("keep me\n")
    operations = [
        {"path": "modified.py", "operation": "modify", "content": "changed\n"},
        {"path": "deleted.py", "operation": "delete", "content": None},
        {"path": "uncreated/nested.py", "operation": "create", "content": "new\n"},
        invalid_operation,
    ]

    with pytest.raises(SystemExit) as exc:
        run_refactoring(target, operations, helper)

    assert exc.value.code == 1
    assert (target / "modified.py").read_text() == "original\n"
    assert (target / "deleted.py").read_text() == "keep me\n"
    assert not (target / "uncreated").exists()
    assert not (target / "refactored_files.json").exists()


def test_valid_relative_operations_and_backslash_paths(tmp_path, run_refactoring):
    (tmp_path / "modified.py").write_text("original\n")
    (tmp_path / "deleted.py").write_text("remove me\n")
    operations = [
        {
            "path": r"generated\nested\created.py",
            "operation": "create",
            "content": "created\n",
        },
        {"path": "modified.py", "operation": "modify", "content": "modified\n"},
        {"path": "deleted.py", "operation": "delete", "content": None},
    ]

    run_refactoring(tmp_path, operations)

    assert (tmp_path / "generated/nested/created.py").read_text() == "created\n"
    assert (tmp_path / "modified.py").read_text() == "modified\n"
    assert not (tmp_path / "deleted.py").exists()
    recorded = json.loads((tmp_path / "refactored_files.json").read_text())
    assert recorded[0]["path"] == "generated/nested/created.py"
    assert recorded[1:] == operations[1:]


def test_in_analysis_symlinks_preserve_file_operation_semantics(
    tmp_path, run_refactoring
):
    package = tmp_path / "package"
    package.mkdir()
    (package / "existing.py").write_text("original\n")
    preserved = package / "preserved.py"
    preserved.write_text("keep referent\n")
    alias = tmp_path / "alias"
    alias.symlink_to(package, target_is_directory=True)
    leaf_link = tmp_path / "leaf-link.py"
    leaf_link.symlink_to(preserved)
    operations = [
        {
            "path": "alias/nested/created.py",
            "operation": "create",
            "content": "created\n",
        },
        {
            "path": "alias/existing.py",
            "operation": "modify",
            "content": "modified\n",
        },
        {"path": "leaf-link.py", "operation": "delete", "content": None},
    ]

    run_refactoring(tmp_path, operations)

    assert alias.is_symlink()
    assert (package / "nested/created.py").read_text() == "created\n"
    assert (package / "existing.py").read_text() == "modified\n"
    assert not leaf_link.is_symlink()
    assert not leaf_link.exists()
    assert preserved.read_text() == "keep referent\n"


@pytest.mark.parametrize(
    "invalid_path",
    [
        pytest.param("absolute-inside", id="absolute-inside-analysis"),
        pytest.param("absolute-outside", id="absolute-outside-analysis"),
        pytest.param(r"C:\outside.py", id="windows-absolute"),
        pytest.param(r"\\server\share\outside.py", id="windows-unc"),
        pytest.param("../outside.py", id="parent-escape"),
        pytest.param(r"..\outside.py", id="backslash-parent-escape"),
        pytest.param("nested/../../outside.py", id="nested-parent-escape"),
        pytest.param("nested/../inside.py", id="parent-component-within-analysis"),
        pytest.param("", id="empty"),
        pytest.param(".", id="analysis-directory"),
        pytest.param(None, id="null"),
        pytest.param(42, id="non-string"),
    ],
)
def test_invalid_path_rejects_entire_batch(tmp_path, run_refactoring, invalid_path):
    target = tmp_path / "analysis"
    target.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside original\n")
    if invalid_path == "absolute-inside":
        invalid_path = str(target / "inside.py")
    elif invalid_path == "absolute-outside":
        invalid_path = str(outside)

    assert_batch_rejected(
        run_refactoring,
        target,
        {"path": invalid_path, "operation": "create", "content": "unsafe\n"},
    )

    assert outside.read_text() == "outside original\n"
    assert not (target / "inside.py").exists()
    assert not (target / "nested").exists()


@pytest.mark.parametrize(
    "invalid_path, operation",
    [
        ("vendor/runtime/engine.py", "modify"),
        ("vendor/runtime", "delete"),
        ("tool-alias/engine.py", "delete"),
        ("vendor/runtime/customer-link.py", "modify"),
    ],
)
def test_configured_helper_checkout_is_protected(
    tmp_path, run_refactoring, invalid_path, operation
):
    helper = tmp_path / "vendor/runtime"
    helper.mkdir(parents=True)
    (helper / "engine.py").write_text("helper original\n")
    customer = tmp_path / "customer.py"
    customer.write_text("customer original\n")
    (tmp_path / "tool-alias").symlink_to(helper, target_is_directory=True)
    (helper / "customer-link.py").symlink_to(customer)

    assert_batch_rejected(
        run_refactoring,
        tmp_path,
        {
            "path": invalid_path,
            "operation": operation,
            "content": None if operation == "delete" else "unsafe\n",
        },
        helper,
    )

    assert (helper / "engine.py").read_text() == "helper original\n"
    assert customer.read_text() == "customer original\n"
    assert (helper / "customer-link.py").is_symlink()


def test_nested_analysis_uses_actual_helper_location(tmp_path, run_refactoring):
    helper = tmp_path / "vendor/runtime"
    helper.mkdir(parents=True)
    (helper / "engine.py").write_text("helper original\n")
    target = tmp_path / "services/customer"
    target.mkdir(parents=True)
    operations = [
        {
            "path": ".refactorer-tool/customer.py",
            "operation": "create",
            "content": "customer = 1\n",
        },
        {
            "path": "vendor/runtime/customer.py",
            "operation": "create",
            "content": "customer = 2\n",
        },
    ]

    run_refactoring(target, operations, helper)

    assert (target / ".refactorer-tool/customer.py").read_text() == "customer = 1\n"
    assert (target / "vendor/runtime/customer.py").read_text() == "customer = 2\n"
    assert (helper / "engine.py").read_text() == "helper original\n"


@pytest.mark.parametrize(
    "link_kind, operation",
    [
        ("file", "modify"),
        ("file", "delete"),
        ("directory", "create"),
        ("dangling-file", "create"),
        ("dangling-directory", "create"),
    ],
)
def test_symlink_escapes_reject_entire_batch(
    tmp_path, run_refactoring, link_kind, operation
):
    target = tmp_path / "analysis"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "existing.py"
    outside_file.write_text("outside original\n")
    link = target / "link"
    if link_kind == "file":
        link.symlink_to(outside_file)
        invalid_path = "link"
    elif link_kind == "directory":
        link.symlink_to(outside, target_is_directory=True)
        invalid_path = "link/new-directory/created.py"
    elif link_kind == "dangling-file":
        link.symlink_to(outside / "missing.py")
        invalid_path = "link"
    else:
        link.symlink_to(outside / "missing-directory", target_is_directory=True)
        invalid_path = "link/created.py"

    assert_batch_rejected(
        run_refactoring,
        target,
        {
            "path": invalid_path,
            "operation": operation,
            "content": None if operation == "delete" else "unsafe\n",
        },
    )

    assert link.is_symlink()
    assert outside_file.read_text() == "outside original\n"
    assert sorted(path.name for path in outside.iterdir()) == ["existing.py"]
