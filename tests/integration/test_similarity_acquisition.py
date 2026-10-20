"""Test Git source loading and production scoring without loading a model."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import correctness_filter
from scripts.correctness_filter import _load_similarity_inputs


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def write(root, path, source):
    file_path = root / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")


def commit(root):
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "user.name=Similarity Tests",
        "-c",
        "user.email=similarity-tests@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Original sources",
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    initialize_repository(root, {})
    return root


def initialize_repository(root, sources):
    git(root, "init", "-q")
    for path, source in sources.items():
        write(root, path, source)
    if sources:
        commit(root)


def write_artifacts(root, modules, refactored, similarity_context=None):
    (root / "modules.json").write_text(json.dumps(modules))
    (root / "refactored_files.json").write_text(json.dumps(refactored))
    proposal = {}
    if similarity_context is not None:
        proposal["similarity_context"] = similarity_context
    (root / "proposal.json").write_text(json.dumps(proposal))


def detector_context(*paths):
    return {
        "File": paths[0],
        "Related Participants": json.dumps([{"file_path": path} for path in paths]),
    }


def serialize_sources(sources):
    return "\n".join(f"# FILE: {path}\n{sources[path]}" for path in sorted(sources))


def install_fake_codebert(monkeypatch, f1):
    calls = []
    tensor_reads = []

    def tensor(name, value):
        def item():
            tensor_reads.append(name)
            return value

        return SimpleNamespace(item=item)

    def score(**kwargs):
        calls.append(kwargs)
        return (
            tensor("precision", 0.11),
            tensor("recall", 0.22),
            tensor("f1", f1),
            tensor("f3", 0.99),
        )

    monkeypatch.setitem(sys.modules, "code_bert_score", SimpleNamespace(score=score))
    return calls, tensor_reads


@pytest.mark.parametrize("target_directory", [".", "nested/project"])
def test_fresh_status_selects_requested_changes_with_target_relative_paths(
    repo, target_directory
):
    target = repo / target_directory
    originals = {
        "anchor.py": "anchor = 1\n",
        "modified.py": "modified = 1\n",
        "pkg/staged.py": "staged = 1\n",
        "removed.py": "removed = 1\n",
        "no_op.py": "no_op = 1\n",
        "unrelated.py": "unrelated = 1\n",
        "README.md": "Original documentation\n",
    }
    for path, source in originals.items():
        write(target, path, source)
    commit(repo)
    candidates = {
        "anchor.py": originals["anchor.py"],
        "modified.py": "modified = 2\n",
        "pkg/staged.py": "staged = 2\n",
        "new/package.py": "created = 2\n",
        "new/intent.py": "created = 3\n",
        "new/staged.py": "created = 4\n",
    }
    for path, source in candidates.items():
        write(target, path, source)
    git(repo, "add", "--", f"{target_directory}/pkg/staged.py")
    git(repo, "add", "-N", "--", f"{target_directory}/new/intent.py")
    git(repo, "add", "--", f"{target_directory}/new/staged.py")
    candidates["new/staged.py"] = "created = 5\n"
    write(target, "new/staged.py", candidates["new/staged.py"])
    (target / "removed.py").unlink()
    write(target, "README.md", "Updated documentation\n")
    # Ignore unrelated files, even when Git reports a change or the proposal
    # gathered their original code.
    (target / "unrelated.py").unlink()
    (target / "unrelated.py").symlink_to(repo.parent / "missing-source.py")
    operations = [
        {"path": "./modified.py", "operation": "modify", "content": "stale"},
        {"path": "pkg\\staged.py", "operation": "modify", "content": "stale"},
        {"path": "removed.py", "operation": "delete"},
        {"path": "new/package.py", "operation": "create", "content": "stale"},
        {"path": "new/intent.py", "operation": "create", "content": "stale"},
        {"path": "new/staged.py", "operation": "create", "content": "stale"},
        {"path": "no_op.py", "operation": "modify", "content": "stale"},
        {"path": "README.md", "operation": "modify", "content": "stale"},
    ]

    before, after = _load_similarity_inputs(
        originals, operations, detector_context("./anchor.py"), target_root=target
    )

    assert before == serialize_sources(
        {path: originals[path] for path in candidates if path in originals}
        | {"removed.py": originals["removed.py"]}
    )
    assert after == serialize_sources(candidates)
    assert "stale" not in after


def test_staged_rename_is_acquired_as_requested_delete_and_create(repo):
    originals = {"anchor.py": "anchor = 1\n", "old.py": "retained = 1\n"}
    for path, source in originals.items():
        write(repo, path, source)
    commit(repo)
    git(repo, "mv", "old.py", "new.py")

    before, after = _load_similarity_inputs(
        originals,
        [
            {"path": "old.py", "operation": "delete"},
            {"path": "new.py", "operation": "create", "content": "stale"},
        ],
        detector_context("anchor.py", "old.py"),
        target_root=repo,
    )

    assert before == serialize_sources(originals)
    assert after == serialize_sources(
        {"anchor.py": originals["anchor.py"], "new.py": originals["old.py"]}
    )


def test_acquisition_normalizes_aliases_and_coalesces_duplicate_operations(repo):
    sources = {"pkg/a.py": "value = 1\n", "b.py": "value = 1\n"}
    for path, source in sources.items():
        write(repo, path, source)
    commit(repo)
    write(repo, "pkg/a.py", "value = 2\n")

    before, after = _load_similarity_inputs(
        {
            "./pkg/a.py": sources["pkg/a.py"],
            "pkg\\a.py": sources["pkg/a.py"],
            "b.py": sources["b.py"],
        },
        [
            {"path": "pkg/./a.py", "operation": "modify"},
            {"path": "pkg\\a.py", "operation": "modify"},
        ],
        detector_context("pkg/./a.py", "pkg\\a.py", "b.py"),
        target_root=repo,
    )

    assert before == serialize_sources(sources)
    assert after == serialize_sources({"pkg/a.py": "value = 2\n", "b.py": sources["b.py"]})


@pytest.mark.parametrize(
    "path",
    [
        "../a.py",
        "a/../../b.py",
        "/tmp/a.py",
        "..",
        "C:\\a.py",
        "C:a.py",
        "\\\\host\\a.py",
        "bad\x00.py",
        "",
        None,
    ],
)
def test_acquisition_rejects_unsafe_participant_and_operation_paths(repo, path):
    sources = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", sources["anchor.py"])
    commit(repo)

    with pytest.raises(ValueError):
        _load_similarity_inputs(sources, [], detector_context(path), target_root=repo)
    with pytest.raises(ValueError):
        _load_similarity_inputs(
            sources,
            [{"path": path, "operation": "modify"}],
            detector_context("anchor.py"),
            target_root=repo,
        )


@pytest.mark.parametrize("conflict", ["original", "operation"])
def test_acquisition_rejects_conflicting_normalized_evidence(repo, conflict):
    sources = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", sources["anchor.py"])
    commit(repo)
    write(repo, "anchor.py", "anchor = 2\n")
    operations = [{"path": "anchor.py", "operation": "modify"}]
    if conflict == "original":
        sources["./anchor.py"] = "conflicting source\n"
    else:
        operations.append({"path": "./anchor.py", "operation": "delete"})

    with pytest.raises(ValueError, match="[Cc]onflicting"):
        _load_similarity_inputs(
            sources, operations, detector_context("anchor.py"), target_root=repo
        )


@pytest.mark.parametrize("operation", ["unknown", None, [], {}])
def test_acquisition_rejects_unsupported_operations(repo, operation):
    sources = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", sources["anchor.py"])
    commit(repo)

    with pytest.raises(ValueError):
        _load_similarity_inputs(
            sources,
            [{"path": "anchor.py", "operation": operation}],
            detector_context("anchor.py"),
            target_root=repo,
        )


def test_acquisition_only_validates_selected_original_source(repo):
    sources = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", sources["anchor.py"])
    commit(repo)
    modules = sources | {
        "../outside.py": "irrelevant",
        "importer.py": None,
        "./importer.py": "conflicting irrelevant source",
    }

    assert _load_similarity_inputs(
        modules, [], detector_context("anchor.py"), target_root=repo
    ) == (serialize_sources(sources), serialize_sources(sources))

    modules["anchor.py"] = None
    with pytest.raises(ValueError, match="original source"):
        _load_similarity_inputs(
            modules, [], detector_context("anchor.py"), target_root=repo
        )


@pytest.mark.parametrize(
    "path",
    [
        "space name.py",
        'quote"name.py',
        "café.py",
        "literal[ab].py",
        ":(glob)*.py",
        "line\nbreak.py",
    ],
)
def test_git_filename_quoting_and_pathspec_magic_do_not_omit_changed_source(
    repo, path
):
    originals = {"anchor.py": "anchor = 1\n", path: "value = 1\n"}
    for filename, source in originals.items():
        write(repo, filename, source)
    commit(repo)
    write(repo, path, "value = 2\n")
    write(repo, "literala.py", "unrequested = 2\n")

    before, after = _load_similarity_inputs(
        originals,
        [{"path": path, "operation": "modify", "content": "stale"}],
        detector_context("anchor.py"),
        target_root=repo,
    )

    assert before == serialize_sources(originals)
    assert after == serialize_sources(
        {"anchor.py": originals["anchor.py"], path: "value = 2\n"}
    )


def test_literal_pathspec_does_not_treat_changed_sibling_as_requested_change(repo):
    originals = {
        "anchor.py": "anchor = 1\n",
        "literal[ab].py": "requested = 1\n",
        "literala.py": "unrequested = 1\n",
    }
    for path, source in originals.items():
        write(repo, path, source)
    commit(repo)
    write(repo, "literala.py", "unrequested = 2\n")

    before, after = _load_similarity_inputs(
        originals,
        [{"path": "literal[ab].py", "operation": "modify", "content": "stale"}],
        detector_context("anchor.py"),
        target_root=repo,
    )

    assert before == after == serialize_sources(
        {"anchor.py": originals["anchor.py"]}
    )


@pytest.mark.parametrize(
    "target_directory, helper_directory",
    [(".", None), (".", "vendor/refactorer"), ("nested/project", "vendor/refactorer")],
)
def test_helper_inside_target_is_excluded(
    repo, target_directory, helper_directory
):
    target = repo / target_directory
    helper_file = f"{helper_directory or '.refactorer-tool'}/helper.py"
    originals = {
        "anchor.py": "anchor = 1\n",
        helper_file: "helper = 1\n",
    }
    for path, source in originals.items():
        write(target, path, source)
    commit(repo)
    write(target, helper_file, "helper = 2\n")
    options = {}
    if helper_directory:
        options["tool_checkout_repo_path"] = (
            Path(target_directory) / helper_directory
        ).as_posix()

    before, after = _load_similarity_inputs(
        originals,
        [
            {
                "path": helper_file,
                "operation": "modify",
                "content": "stale",
            }
        ],
        detector_context("anchor.py", helper_file),
        target_root=target,
        **options,
    )

    assert before == after == serialize_sources(
        {"anchor.py": originals["anchor.py"]}
    )


def test_helper_outside_nested_target_does_not_exclude_same_target_relative_prefix(
    repo,
):
    target = repo / "nested/project"
    originals = {
        "anchor.py": "anchor = 1\n",
        ".refactorer-tool/customer.py": "customer = 1\n",
    }
    for path, source in originals.items():
        write(target, path, source)
    write(repo, ".refactorer-tool/helper.py", "helper = 1\n")
    commit(repo)
    write(target, ".refactorer-tool/customer.py", "customer = 2\n")
    write(repo, ".refactorer-tool/helper.py", "helper = 2\n")

    before, after = _load_similarity_inputs(
        originals,
        [
            {
                "path": ".refactorer-tool/customer.py",
                "operation": "modify",
                "content": "stale",
            }
        ],
        detector_context("anchor.py"),
        target_root=target,
        tool_checkout_repo_path=".refactorer-tool",
    )

    assert before == serialize_sources(originals)
    assert after == serialize_sources(
        {
            "anchor.py": originals["anchor.py"],
            ".refactorer-tool/customer.py": "customer = 2\n",
        }
    )


def test_git_repository_error_rejects_source_acquisition(tmp_path):
    write(tmp_path, "anchor.py", "anchor = 1\n")

    with pytest.raises(subprocess.CalledProcessError):
        _load_similarity_inputs(
            {"anchor.py": "anchor = 1\n"}, [], detector_context("anchor.py"), target_root=tmp_path
        )


def test_git_status_failure_rejects_source_acquisition(repo):
    write(repo, "anchor.py", "anchor = 1\n")
    commit(repo)
    write(repo, "anchor.py", "anchor = 2\n")
    (repo / ".git/index").write_bytes(b"invalid Git index")

    with pytest.raises(subprocess.CalledProcessError):
        _load_similarity_inputs(
            {"anchor.py": "anchor = 1\n"},
            [{"path": "anchor.py", "operation": "modify", "content": "stale"}],
            detector_context("anchor.py"),
            target_root=repo,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"Related Participants": "invalid JSON"},
        {"Related Participants": "[]"},
        {"Related Participants": '[{"file_path": "report.csv"}]'},
        {"Related Participants": '[{"name": "anchor"}]'},
        {
            "File": "../outside.py",
            "Related Participants": '[{"file_path": "anchor.py"}]',
        },
    ],
)
def test_acquisition_requires_valid_detector_participants(repo, metadata):
    originals = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", originals["anchor.py"])
    commit(repo)

    with pytest.raises((KeyError, TypeError, ValueError)):
        _load_similarity_inputs(originals, [], metadata, target_root=repo)


def test_acquisition_rejects_candidate_symlink_outside_target(repo):
    originals = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", originals["anchor.py"])
    commit(repo)
    outside = repo.parent / "outside.py"
    outside.write_text("anchor = 2\n")
    (repo / "anchor.py").unlink()
    (repo / "anchor.py").symlink_to(outside)

    with pytest.raises(ValueError):
        _load_similarity_inputs(
            originals,
            [{"path": "anchor.py", "operation": "modify", "content": "stale"}],
            detector_context("anchor.py"),
            target_root=repo,
        )


@pytest.mark.parametrize(
    "state, error",
    [
        ("missing", FileNotFoundError),
        ("directory", IsADirectoryError),
        ("invalid_utf8", UnicodeDecodeError),
        ("helper_alias", ValueError),
        ("broken_symlink", FileNotFoundError),
        ("symlink_loop", OSError),
        ("requested_delete_still_exists", ValueError),
    ],
)
def test_acquisition_rejects_unusable_or_contradictory_candidate_source(
    repo, state, error
):
    originals = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", originals["anchor.py"])
    commit(repo)
    candidate = repo / "anchor.py"
    operation = "modify"
    if state in {
        "missing",
        "directory",
        "helper_alias",
        "broken_symlink",
        "symlink_loop",
    }:
        candidate.unlink()
    if state == "directory":
        candidate.mkdir()
    elif state == "invalid_utf8":
        candidate.write_bytes(b"\xff")
    elif state == "helper_alias":
        write(repo, ".refactorer-tool/helper.py", "helper = 2\n")
        candidate.symlink_to(repo / ".refactorer-tool/helper.py")
    elif state == "broken_symlink":
        candidate.symlink_to(repo / "missing.py")
    elif state == "symlink_loop":
        candidate.symlink_to(candidate)
    elif state == "requested_delete_still_exists":
        candidate.write_text("anchor = 2\n")
        operation = "delete"

    with pytest.raises(error):
        _load_similarity_inputs(
            originals,
            [{"path": "anchor.py", "operation": operation, "content": "stale"}],
            detector_context("anchor.py"),
            target_root=repo,
        )



def test_main_scores_complete_disk_sources(tmp_path, monkeypatch, fixtures_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)

    case = json.loads(
        (fixtures_dir / "similarity_cases.json").read_text()
    )["consolidation"]
    initialize_repository(tmp_path, case["modules"])
    for path in case["changed_paths"]:
        if path not in case["candidate_sources"]:
            (tmp_path / path).unlink()
        else:
            (tmp_path / path).write_text(case["candidate_sources"][path])
    # Score the file on disk even when the requested content is stale.
    case["refactored"][1]["content"] = "stale_request_payload = True\n"
    write_artifacts(tmp_path, case["modules"], case["refactored"], case["smell_details"])
    calls, tensor_reads = install_fake_codebert(monkeypatch, 0.75)

    assert correctness_filter.main() == 0

    before = {
        path: case["modules"][path]
        for path in ("legacy_rules.py", "service.py", "shared_rules.py")
    }
    after = {
        path: case["candidate_sources"][path]
        for path in ("service.py", "shared_rules.py")
    }
    assert calls == [
        {
            "cands": [serialize_sources(after)],
            "refs": [serialize_sources(before)],
            "lang": "python",
        }
    ]
    assert tensor_reads == ["f1"]


@pytest.mark.parametrize(
    "operation, original, candidate, padding_lines",
    [
        ("modify", "value = 1\n", "value = 2\n", 0),
        ("modify", "value = 1\n", "value = 2\n", 30),
        ("modify", "value = 1\n", "value = 1\n", 0),
        ("create", None, "value = 2\n", 0),
        ("delete", "value = 1\n", None, 0),
    ],
    ids=["tiny-files", "one-line-edit", "no-op", "create-only", "delete-only"],
)
def test_main_scores_full_source_for_any_change_size(
    repo, monkeypatch, operation, original, candidate, padding_lines
):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    padding = "unchanged = True\n" * padding_lines
    before = {"anchor.py": "anchor = 1\n"}
    after = before.copy()
    if original is not None:
        before["other.py"] = padding + original
    if candidate is not None:
        after["other.py"] = padding + candidate
    for path, source in before.items():
        write(repo, path, source)
    commit(repo)
    if operation == "delete":
        (repo / "other.py").unlink()
    else:
        write(repo, "other.py", after["other.py"])
    write_artifacts(
        repo, before,
        [{"path": "other.py", "operation": operation, "content": "stale"}],
        detector_context(*before),
    )
    calls, _ = install_fake_codebert(monkeypatch, 0.75)

    assert correctness_filter.main() == 0
    assert calls == [{
        "cands": [serialize_sources(after)],
        "refs": [serialize_sources(before)],
        "lang": "python",
    }]


def test_main_skips_scoring_when_all_participants_are_deleted(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    sources = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", sources["anchor.py"])
    commit(repo)
    (repo / "anchor.py").unlink()
    write_artifacts(
        repo, sources, [{"path": "anchor.py", "operation": "delete"}],
        detector_context("anchor.py"),
    )
    calls, _ = install_fake_codebert(monkeypatch, 0.99)

    assert correctness_filter.main() == 0
    assert calls == []

    # A missing original is an error even when there is no remaining source.
    (repo / "modules.json").write_text("{}")
    assert correctness_filter.main() == 1
    assert calls == []


@pytest.mark.parametrize("state", ["unstaged", "staged", "recreated"])
def test_main_rejects_existing_file_labeled_create(repo, monkeypatch, caplog, state):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)
    modules = {"anchor.py": "anchor = 1\n"}
    write(repo, "anchor.py", modules["anchor.py"])
    write(repo, "existing.py", "value = 1\n")
    commit(repo)
    if state == "recreated":
        git(repo, "rm", "existing.py")
    write(repo, "existing.py", "value = 2\n")
    if state == "staged":
        git(repo, "add", "existing.py")
    write_artifacts(
        repo, modules,
        [{"path": "existing.py", "operation": "create", "content": "stale"}],
        detector_context("anchor.py"),
    )
    calls, _ = install_fake_codebert(monkeypatch, 0.99)

    assert correctness_filter.main() == 1
    assert calls == []
    assert "Created file already existed: existing.py" in caplog.text


def test_main_refreshes_eligible_changes_after_pytest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    original, modified = "value = 1\n", "value = 2\n"
    modules = {"a.py": original, "caller.py": "def caller():\n    return 'before'\n"}
    initialize_repository(tmp_path, modules)
    (tmp_path / "a.py").write_text(modified)
    caller_after = "def caller():\n    return 'created during pytest'\n"
    refactored = [
        {"path": "a.py", "operation": "modify", "content": modified},
        {"path": "caller.py", "operation": "modify", "content": caller_after},
    ]
    write_artifacts(tmp_path, modules, refactored, detector_context("a.py"))

    def run_tests():
        assert (tmp_path / "caller.py").read_text() == modules["caller.py"]
        (tmp_path / "caller.py").write_text(caller_after)
        return {"status": "passed", "reason": "Tests passed."}

    monkeypatch.setattr(correctness_filter, "run_repository_tests", run_tests)
    calls, _ = install_fake_codebert(monkeypatch, 0.99)

    assert correctness_filter.main() == 0
    assert calls[0]["refs"] == [serialize_sources(modules)]
    assert calls[0]["cands"] == [
        serialize_sources({"a.py": modified, "caller.py": caller_after})
    ]


@pytest.mark.parametrize(
    "failure", ["metadata", "original", "candidate", "symlink_loop", "git"]
)
def test_main_input_evidence_failures_reject_without_scoring(
    tmp_path, monkeypatch, caplog, failure
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    original, modified = "value = 1\n", "value = 2\n"
    modules = {"a.py": original, "survivor.py": "survivor = True\n"}
    if failure == "git":
        for path, source in modules.items():
            (tmp_path / path).write_text(source)
    else:
        initialize_repository(tmp_path, modules)
    (tmp_path / "a.py").write_text(modified)
    context = detector_context("a.py", "survivor.py")
    if failure == "metadata":
        context = None
    elif failure == "original":
        modules.pop("survivor.py")
    elif failure == "candidate":
        (tmp_path / "survivor.py").unlink()
    elif failure == "symlink_loop":
        survivor = tmp_path / "survivor.py"
        survivor.unlink()
        survivor.symlink_to(survivor)
    write_artifacts(
        tmp_path,
        modules,
        [{"path": "a.py", "operation": "modify", "content": modified}],
        context,
    )
    calls, _ = install_fake_codebert(monkeypatch, 0.99)

    assert correctness_filter.main() == 1
    assert calls == []
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.parametrize("helper_path", [None, "tools/runtime"])
def test_main_passes_helper_checkout_setting_to_source_adapter(
    tmp_path, monkeypatch, helper_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    if helper_path is None:
        monkeypatch.delenv("REFACTORER_TOOL_CHECKOUT_PATH", raising=False)
    else:
        monkeypatch.setenv("REFACTORER_TOOL_CHECKOUT_PATH", helper_path)
    original, modified = "value = 1\n", "value = 2\n"
    modules = {"a.py": original}
    refactored = [{"path": "a.py", "operation": "modify", "content": modified}]
    context = detector_context("a.py")
    write_artifacts(tmp_path, modules, refactored, context)
    acquired = []

    def load_current(modules_arg, operations_arg, smell_arg, **kwargs):
        acquired.append((modules_arg, operations_arg, smell_arg, kwargs))
        return "complete before", "complete after"

    monkeypatch.setattr(correctness_filter, "_load_similarity_inputs", load_current)
    calls, _ = install_fake_codebert(monkeypatch, 1.0)

    assert correctness_filter.main() == 0
    assert acquired == [
        (
            modules,
            refactored,
            context,
            {
                "target_root": tmp_path,
                "tool_checkout_repo_path": helper_path or ".refactorer-tool",
            },
        )
    ]
    assert calls[0]["cands"] == ["complete after"]
    assert calls[0]["refs"] == ["complete before"]
