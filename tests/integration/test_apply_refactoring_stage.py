import json
import sys
from types import SimpleNamespace

import pytest

from scripts import apply_refactoring, correctness_filter, llm_clients


@pytest.mark.parametrize(
    "include_originals", [False, True], ids=["outside-context", "in-context"]
)
def test_apply_refactoring_applies_file_operations(
    tmp_path, monkeypatch, include_originals
):
    plan_path = tmp_path / "proposal.json"
    modules_path = tmp_path / "modules.json"
    created_file = tmp_path / "created.py"
    modified_file = tmp_path / "modified.py"
    deleted_file = tmp_path / "delete_me.py"

    modified_file.write_text("original\n")
    deleted_file.write_text("to remove\n")

    plan_path.write_text(json.dumps({"actions": [{"dummy": True}]}))
    original_sources = {"modified.py": "original\n", "delete_me.py": "to remove\n"}
    modules = {"module": "code"}
    if include_originals:
        modules.update(original_sources)
    modules_path.write_text(json.dumps(modules))

    operations = [
        {"path": "created.py", "operation": "create", "content": "created\n"},
        {"path": "modified.py", "operation": "modify", "content": "modified\n"},
        {"path": "delete_me.py", "operation": "delete", "content": None},
    ]

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(apply_refactoring, "execute_refactor", lambda plan, modules, importers_map: operations)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            "uuid",
            "--proposal-file",
            str(plan_path),
            "--modules-file",
            str(modules_path),
        ],
    )
    monkeypatch.chdir(tmp_path)

    apply_refactoring.main()
    assert created_file.read_text() == "created\n"
    assert modified_file.read_text() == "modified\n"
    assert not deleted_file.exists()

    written = json.loads((tmp_path / "refactored_files.json").read_text())
    assert written == operations

    snapshots = json.loads(modules_path.read_text())
    assert snapshots == {"module": "code", **original_sources}
    before, after = correctness_filter.build_similarity_inputs(
        snapshots,
        {"created.py": created_file.read_text(), "modified.py": modified_file.read_text()},
        set(),
        {item["path"]: item["operation"] for item in written},
    )
    assert before == "# FILE: delete_me.py\nto remove\n\n# FILE: modified.py\noriginal\n"
    assert after == "# FILE: created.py\ncreated\n\n# FILE: modified.py\nmodified\n"


def test_apply_refactoring_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proposal.json").write_text(json.dumps({"actions": []}))
    (tmp_path / "modules.json").write_text(json.dumps([]))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            "uuid",
            "--proposal-file",
            str(tmp_path / "proposal.json"),
            "--modules-file",
            str(tmp_path / "modules.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        apply_refactoring.main()

    assert exc.value.code == 1


def test_apply_refactoring_handles_missing_delete(tmp_path, monkeypatch):
    plan_path = tmp_path / "proposal.json"
    modules_path = tmp_path / "modules.json"
    plan_path.write_text(json.dumps({"actions": []}))
    modules_path.write_text(json.dumps({}))

    operations = [
        {"path": "new_dir/file.py", "operation": "create", "content": "print('new')\n"},
        {"path": "absent.py", "operation": "delete", "content": None},
    ]

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        apply_refactoring,
        "execute_refactor",
        lambda plan, modules, importers_map: operations,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            "uuid",
            "--proposal-file",
            str(plan_path),
            "--modules-file",
            str(modules_path),
        ],
    )
    monkeypatch.chdir(tmp_path)

    apply_refactoring.main()

    created = tmp_path / "new_dir" / "file.py"
    assert created.exists()


def test_apply_refactoring_ignores_unknown_operation(tmp_path, monkeypatch):
    plan_path = tmp_path / "proposal.json"
    modules_path = tmp_path / "modules.json"
    plan_path.write_text(json.dumps({"actions": []}))
    modules_path.write_text(json.dumps({}))

    operations = [
        {"path": "ignore.py", "operation": "rename", "content": "data"},
        {"path": "valid.py", "operation": "create", "content": "ok\n"},
    ]

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(apply_refactoring, "execute_refactor", lambda plan, modules, importers_map: operations)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            "uuid",
            "--proposal-file",
            str(plan_path),
            "--modules-file",
            str(modules_path),
        ],
    )
    monkeypatch.chdir(tmp_path)

    apply_refactoring.main()

    assert not (tmp_path / "ignore.py").exists()
    assert (tmp_path / "valid.py").read_text() == "ok\n"


@pytest.mark.parametrize(
    "complete_plan",
    [
        pytest.param(
            {
                "strategy": "ExtractUtility",
                "explanation": "Move shared item validation to a neutral utility module so pricing no longer imports orders; orders can still call pricing.quote without a cycle.",
                "actions": ["move_func", "modify_module", "modify_module"],
            },
            id="recorded-local-plan",
        ),
        pytest.param(
            {
                "strategy": "ExtractUtility",
                "explanation": "Extract shared validation into a utility module.",
                "actions": [
                    {
                        "type": "move_func",
                        "source": {
                            "module": "orders.py",
                            "symbols": ["validate_item"],
                        },
                        "destination": {"module": "utils.py"},
                        "details": {
                            "flags": [True, False, None],
                            "line": 7,
                            "ratio": 0.5,
                            "label": "item",
                        },
                    }
                ],
                "importers_by_participant": {"orders.py": ["app.py"]},
            },
            id="nested-action-objects",
        ),
        pytest.param(
            {
                "strategy": "CollapseHierarchy",
                "explanation": "Keep the canonical implementation.",
                "actions": ["remove_module", "modify_module"],
                "canonical_module": "pricing.py",
                "importers_by_participant": {},
            },
            id="canonical-module-with-empty-importers",
        ),
        pytest.param(
            {
                "strategy": "Interface",
                "explanation": "Invert the shared dependency.",
                "actions": ["create_interface", "modify_module"],
                "additional_decisions": {
                    "groups": [["a", "b"]],
                    "enabled": True,
                    "note": None,
                },
                "importers_by_participant": {"pricing.py": ["orders.py"]},
            },
            id="additional-nested-plan-field",
        ),
    ],
)
def test_apply_refactoring_transmits_complete_plan(
    tmp_path, monkeypatch, complete_plan
):
    plan_path = tmp_path / "proposal.json"
    modules_path = tmp_path / "modules.json"
    original_modules = {
        "orders.py": "from pricing import quote\n",
        "pricing.py": "def quote(item):\n    return item['price']\n",
        "app.py": "import orders\n",
    }
    plan_path.write_text(json.dumps(complete_plan))
    modules_path.write_text(json.dumps(original_modules))
    captured_requests = []
    deleted_file = tmp_path / "obsolete.py"
    deleted_file.write_text("# obsolete module\n")
    operations = [
        {
            "path": "result.py",
            "operation": "create",
            "content": "# deterministic transport fixture\n",
        },
        {"path": "obsolete.py", "operation": "delete", "content": None},
    ]

    def fake_create(**kwargs):
        captured_requests.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(type="reasoning"),
                SimpleNamespace(
                    type="function_call",
                    name="submit_refactored_files",
                    arguments=json.dumps({"files": operations}),
                ),
            ],
        )

    fake_client = SimpleNamespace(
        responses=SimpleNamespace(create=fake_create)
    )
    monkeypatch.setattr(llm_clients.openai, "OpenAI", lambda: fake_client)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_refactoring.py",
            "--smell-uuid",
            "uuid",
            "--proposal-file",
            str(plan_path),
            "--modules-file",
            str(modules_path),
        ],
    )
    monkeypatch.chdir(tmp_path)

    apply_refactoring.main()

    assert len(captured_requests) == 1
    request = captured_requests[0]
    payload = json.loads(request["input"])
    assert payload["plan"] == complete_plan
    assert payload == {
        "plan": complete_plan,
        "modules": original_modules,
        "importers_by_participant": complete_plan.get("importers_by_participant", {}),
    }
    assert "plan_actions" not in payload
    assert json.loads(plan_path.read_text()) == complete_plan
    assert json.loads(modules_path.read_text()) == {
        **original_modules,
        "obsolete.py": "# obsolete module\n",
    }
    assert json.loads((tmp_path / "refactored_files.json").read_text()) == operations
    assert (tmp_path / "result.py").read_text() == operations[0]["content"]
    assert not deleted_file.exists()

    assert request["instructions"] == """You are a precise code-transformation assistant. Implement the supplied refactoring plan.

INPUT FORMAT:
- plan: Complete refactoring plan, including its strategy, explanation, actions, and other planning decisions
- modules: Dictionary mapping file paths to their current contents
- importers_by_participant: Dictionary mapping each participant file to files that import it (only includes files whose content is in modules)

Follow the selected strategy and use the explanation and other planning decisions to guide how you carry out the actions.
If canonical_module is supplied, it identifies the file to keep after consolidation.

INVARIANTS:
- No dangling imports after changes
- If you delete/move a file, you MUST update ALL importers listed in importers_by_participant
- Refactor only; no feature additions; no stubs

For each file, clearly indicate the operation type:
- 'create': for new files that don't exist yet
- 'modify': for existing files that need changes
- 'delete': for files that should be removed

Output your response ONLY via the submit_refactored_files function."""
    assert request["tools"] == llm_clients.IMPLEMENTATION_TOOL_SCHEMA
    assert request["tool_choice"] == {
        "type": "function",
        "name": "submit_refactored_files",
    }
    assert request["parallel_tool_calls"] is False
    assert request["store"] is False
