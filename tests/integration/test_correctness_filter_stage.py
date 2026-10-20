import json

import pytest

from scripts import correctness_filter


# --- Correctness filter: main ---


@pytest.fixture
def similarity_not_run(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Similarity must not run for this case")

    monkeypatch.setattr(correctness_filter, "_load_similarity_inputs", unexpected)
    monkeypatch.setattr(correctness_filter, "check_similarity", unexpected)


def test_main_fails_on_missing_modules_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )

    # Only create refactored_files.json, not modules.json
    (tmp_path / "refactored_files.json").write_text("[]")

    assert correctness_filter.main() == 1
    assessment = json.loads((tmp_path / "validation_assessment.json").read_text())
    assert assessment["decision"] == "rejected"
    assert "FileNotFoundError" in assessment["reason"]


def test_main_fails_on_missing_refactored_files_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )

    # Only create modules.json, not refactored_files.json
    (tmp_path / "modules.json").write_text("{}")

    assert correctness_filter.main() == 1
    assessment = json.loads((tmp_path / "validation_assessment.json").read_text())
    assert assessment["decision"] == "rejected"
    assert "FileNotFoundError" in assessment["reason"]


def test_main_returns_1_on_test_failure(tmp_path, monkeypatch, similarity_not_run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "failed", "reason": "Tests failed."},
    )

    result = correctness_filter.main()
    assert result == 1


def test_main_returns_1_on_strategy_policy_violation(
    tmp_path, monkeypatch, similarity_not_run
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )

    (tmp_path / "modules.json").write_text('{"a.py": "code"}')
    (tmp_path / "refactored_files.json").write_text('[{"path": "a.py", "operation": "delete", "content": null}]')
    (tmp_path / "proposal.json").write_text('{"strategy": "CollapseHierarchy"}')
    monkeypatch.setattr(
        correctness_filter,
        "check_deletion_safety",
        lambda *args, **kwargs: pytest.fail("Strategy failure must stop validation"),
    )

    result = correctness_filter.main()
    assert result == 1


def test_main_returns_1_on_deletion_safety_failure(
    tmp_path, monkeypatch, similarity_not_run
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )

    # Create files in tmp_path for deletion safety check
    (tmp_path / "deleted.py").write_text("def func(): pass")
    (tmp_path / "consumer.py").write_text("from deleted import func")

    (tmp_path / "modules.json").write_text('{"deleted.py": "def func(): pass"}')
    # Use relative path for the deleted file so check_deletion_safety can match
    (tmp_path / "refactored_files.json").write_text(
        '[{"path": "deleted.py", "operation": "delete", "content": null}]'
    )

    result = correctness_filter.main()
    assert result == 1


@pytest.mark.parametrize("sources", [("", "after"), ("before", " \n")])
def test_main_skips_similarity_when_a_full_source_side_is_empty(
    tmp_path, monkeypatch, sources
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    (tmp_path / "modules.json").write_text("{}")
    (tmp_path / "refactored_files.json").write_text("[]")
    loads = []

    def load_inputs(*args, **kwargs):
        loads.append(sources)
        return sources

    monkeypatch.setattr(correctness_filter, "_load_similarity_inputs", load_inputs)
    monkeypatch.setattr(
        correctness_filter,
        "check_similarity",
        lambda *args: pytest.fail("An empty source side cannot be compared"),
    )

    assert correctness_filter.main() == 0
    assert loads == [sources]


def test_main_returns_1_on_similarity_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)

    original = "def value(): return 1\n"
    modified = "def value(): return 2\n"

    (tmp_path / "modules.json").write_text(json.dumps({"a.py": original}))
    (tmp_path / "refactored_files.json").write_text(
        json.dumps([{"path": "a.py", "operation": "modify", "content": modified}])
    )

    monkeypatch.setattr(
        correctness_filter,
        "_load_similarity_inputs",
        lambda *args, **kwargs: (original, modified),
    )
    similarity_calls = []

    def check_similarity(candidate, reference, threshold):
        similarity_calls.append((candidate, reference, threshold))
        return False, 0.50

    monkeypatch.setattr(correctness_filter, "check_similarity", check_similarity)

    result = correctness_filter.main()
    assert result == 1
    assert similarity_calls == [(modified, original, 0.75)]


def test_main_uses_similarity_threshold_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIMILARITY_THRESHOLD", "0.99")
    monkeypatch.setattr(
        correctness_filter,
        "run_repository_tests",
        lambda: {"status": "passed", "reason": "Tests passed."},
    )

    original = "def value(): return 1\n"
    modified = "def value(): return 2\n"

    (tmp_path / "modules.json").write_text(json.dumps({"a.py": original}))
    (tmp_path / "refactored_files.json").write_text(
        json.dumps([{"path": "a.py", "operation": "modify", "content": modified}])
    )
    monkeypatch.setattr(
        correctness_filter,
        "_load_similarity_inputs",
        lambda *args, **kwargs: (original, modified),
    )

    captured_threshold = None

    def mock_check(candidate, reference, threshold):
        nonlocal captured_threshold
        captured_threshold = threshold
        return True, 0.99

    monkeypatch.setattr(correctness_filter, "check_similarity", mock_check)

    result = correctness_filter.main()
    assert captured_threshold == 0.99
    assert result == 0
