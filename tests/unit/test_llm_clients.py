import copy
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from openai.types.responses import Response

from scripts import llm_clients


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    for variable in (
        "OPENAI_BASE_URL",
        "REASONER_MODEL",
        "CODER_MODEL",
        "REASONER_REASONING_EFFORT",
        "CODER_REASONING_EFFORT",
    ):
        monkeypatch.delenv(variable, raising=False)


def response_with(output, **overrides):
    """Build SDK response objects without making an API request."""
    payload = {
        "id": "resp_test",
        "created_at": 0,
        "model": llm_clients.REASONER_MODEL,
        "object": "response",
        "output": output,
        "parallel_tool_calls": False,
        "status": "completed",
        "tool_choice": "auto",
        "tools": [],
    }
    payload.update(overrides)
    return Response.model_validate(payload)


def reasoning_item():
    return {"id": "rs_test", "type": "reasoning", "summary": []}


def text_item(text):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_item(arguments, name="submit_refactored_files"):
    return {
        "id": "fc_test",
        "type": "function_call",
        "call_id": "call_test",
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


@pytest.fixture
def install_response(monkeypatch):
    def install(response=None, error=None):
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            if error is not None:
                raise error
            return response

        # There is deliberately no Chat Completions endpoint on this client.
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        monkeypatch.setattr(llm_clients.openai, "OpenAI", lambda: client)
        return requests

    return install


@pytest.mark.parametrize("smell_type", sorted(llm_clients.REASONER_PROMPTS))
def test_planner_uses_open_json_schema_and_preserves_complete_plan(
    install_response, smell_type
):
    plan = {
        "strategy": "ExtractUtility",
        "explanation": "Move the shared helper to remove the dependency.",
        "actions": [
            {
                "type": "move_func",
                "targets": ["a.py", "b.py"],
                "description": "Move the helper from a.py to shared.py.",
                "rationale": "Let both modules use it without importing each other.",
            }
        ],
        "additional_decisions": {"enabled": True, "note": None},
        "canonical_module": "shared.py",
    }
    requests = install_response(
        response_with([reasoning_item(), text_item(json.dumps(plan))])
    )
    context = "Dependency graph: a -> b -> a\nSource: café = 1\n"

    assert llm_clients.generate_refactor_plan(smell_type, context) == plan

    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == llm_clients.REASONER_MODEL
    assert request["instructions"] == llm_clients.REASONER_PROMPTS[smell_type]
    assert request["input"] == context
    assert request["reasoning"] == {"effort": "xhigh"}
    assert request["text"] == {
        "format": {
            "type": "json_schema",
            "name": "refactor_plan",
            "schema": {"type": "object", "additionalProperties": True},
            "strict": False,
        }
    }
    assert request["store"] is False
    assert "previous_response_id" not in request



@pytest.mark.parametrize("stage", ["planner", "executor"])
def test_empty_environment_keeps_defaults(monkeypatch, install_response, stage):
    prefix = "REASONER" if stage == "planner" else "CODER"
    monkeypatch.setenv(f"{prefix}_MODEL", "")
    monkeypatch.setenv(f"{prefix}_REASONING_EFFORT", "")
    output = text_item("{}") if stage == "planner" else function_item('{"files": []}')
    requests = install_response(response_with([output]))
    call_stage(stage)
    assert requests[0]["model"] == "gpt-5.6-luna"
    assert requests[0]["reasoning"] == {
        "effort": "xhigh" if stage == "planner" else "low"
    }


def test_unknown_smell_is_rejected_before_client_construction(monkeypatch):
    def unexpected_client():
        pytest.fail("Unknown smells must not need credentials or construct a client")

    monkeypatch.setattr(llm_clients.openai, "OpenAI", unexpected_client)

    with pytest.raises(ValueError, match="Unknown smell type: unsupported"):
        llm_clients.generate_refactor_plan("unsupported", "context")


@pytest.mark.parametrize(
    "output",
    [
        pytest.param([], id="missing-text"),
        pytest.param([text_item("")], id="empty-text"),
        pytest.param([text_item("  \n")], id="whitespace-text"),
        pytest.param([text_item("{")], id="malformed-json"),
        pytest.param([text_item("[]")], id="array"),
        pytest.param([text_item("null")], id="null"),
        pytest.param([text_item('"plan"')], id="string"),
    ],
)
def test_planner_rejects_invalid_output_with_chained_stage_error(
    install_response, output
):
    install_response(response_with(output))

    with pytest.raises(RuntimeError, match="^Failed to generate refactor plan:") as exc:
        llm_clients.generate_refactor_plan("cyclic_dependency", "context")

    assert exc.value.__cause__ is not None


def test_executor_preserves_operations_and_implementation_input(install_response):
    operations = [
        {
            "path": "shared.py",
            "operation": "create",
            "content": "def quote():\n    return 1\n",
        },
        {"path": "wrapper.py", "operation": "delete", "content": None},
        {
            "path": "app.py",
            "operation": "modify",
            "content": "from shared import quote\n",
        },
    ]
    plan = {
        "strategy": "CollapseHierarchy",
        "canonical_module": "shared.py",
        "explanation": "Keep the canonical implementation.",
        "actions": [{"operation": "remove_module", "path": "wrapper.py"}],
        "additional_decisions": {"groups": [["a", "b"]], "note": None},
        "importers_by_participant": {"wrapper.py": ["app.py"]},
    }
    implementation_plan = copy.deepcopy(plan)
    plan["similarity_context"] = {
        "File": "wrapper.py",
        "Related Participants": '[{"file_path": "wrapper.py"}]',
    }
    modules = {"wrapper.py": "from shared import quote\n", "app.py": "import wrapper\n"}
    importers = {"wrapper.py": ["app.py"]}
    inputs_before = copy.deepcopy((plan, modules, importers))
    requests = install_response(
        response_with(
            [
                reasoning_item(),
                text_item(""),
                function_item(json.dumps({"files": operations})),
            ]
        )
    )

    assert llm_clients.execute_refactor(plan, modules, importers) == operations

    assert (plan, modules, importers) == inputs_before
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == llm_clients.CODER_MODEL
    assert request["reasoning"] == {"effort": "low"}
    assert request["store"] is False
    assert request["parallel_tool_calls"] is False
    assert request["tool_choice"] == {
        "type": "function",
        "name": "submit_refactored_files",
    }
    assert json.loads(request["input"]) == {
        "plan": implementation_plan,
        "modules": modules,
        "importers_by_participant": importers,
    }
    assert "complete refactoring plan" in request["instructions"].lower()
    assert "No dangling imports" in request["instructions"]
    assert "previous_response_id" not in request
    assert request["tools"] == llm_clients.IMPLEMENTATION_TOOL_SCHEMA
    assert len(request["tools"]) == 1
    tool = request["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "submit_refactored_files"
    assert tool["strict"] is False
    file_properties = tool["parameters"]["properties"]["files"]["items"]["properties"]
    assert set(file_properties["content"]["type"]) == {"string", "null"}


def test_executor_accepts_empty_operations(install_response):
    install_response(response_with([function_item('{"files": []}')]))

    assert llm_clients.execute_refactor({}, {}, {}) == []


@pytest.mark.parametrize(
    "output",
    [
        pytest.param([], id="missing-call"),
        pytest.param([reasoning_item()], id="only-reasoning"),
        pytest.param([text_item('{"files": []}')], id="text-instead-of-call"),
        pytest.param(
            [function_item('{"files": []}', "another_tool")], id="unexpected-name"
        ),
        pytest.param(
            [function_item('{"files": []}'), function_item('{"files": []}')],
            id="duplicate-calls",
        ),
        pytest.param(
            [
                function_item('{"files": []}'),
                function_item('{"files": []}', "another_tool"),
            ],
            id="expected-and-unexpected-calls",
        ),
    ],
)
def test_executor_rejects_invalid_function_calls(install_response, output):
    install_response(response_with(output))

    with pytest.raises(RuntimeError, match="^Failed to execute refactor:") as exc:
        llm_clients.execute_refactor({}, {}, {})

    assert exc.value.__cause__ is not None


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param("", id="empty-arguments"),
        pytest.param("{", id="malformed-json"),
        pytest.param("null", id="null-payload"),
        pytest.param("[]", id="array-payload"),
        pytest.param("{}", id="missing-files"),
        pytest.param('{"files": null}', id="null-files"),
        pytest.param('{"files": {}}', id="object-files"),
        pytest.param('{"files": "files"}', id="string-files"),
        pytest.param('{"files": [null]}', id="null-operation"),
        pytest.param('{"files": [{}, "operation"]}', id="mixed-operation-types"),
    ],
)
def test_executor_rejects_invalid_argument_shapes(install_response, arguments):
    install_response(response_with([function_item(arguments)]))

    with pytest.raises(RuntimeError, match="^Failed to execute refactor:") as exc:
        llm_clients.execute_refactor({}, {}, {})

    assert exc.value.__cause__ is not None


@pytest.mark.parametrize("stage", ["planner", "executor"])
@pytest.mark.parametrize(
    "overrides, detail",
    [
        pytest.param(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "max_output_tokens",
            id="token-limit",
        ),
        pytest.param(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            },
            "content_filter",
            id="content-filter",
        ),
        pytest.param(
            {
                "status": "failed",
                "error": {"code": "server_error", "message": "API unavailable"},
            },
            "API unavailable",
            id="failed",
        ),
        pytest.param({"status": "in_progress"}, "in_progress", id="in-progress"),
        pytest.param({"status": "queued"}, "queued", id="queued"),
        pytest.param({"status": "cancelled"}, "cancelled", id="cancelled"),
    ],
)
def test_noncompleted_responses_are_rejected_before_payload_parsing(
    install_response, stage, overrides, detail
):
    # Malformed payloads distinguish status handling from JSON parsing errors.
    output = [text_item("{")] if stage == "planner" else [function_item("{")]
    install_response(response_with(output, **overrides))

    with pytest.raises(RuntimeError, match=stage_error_prefix(stage)) as exc:
        call_stage(stage)

    assert detail in str(exc.value)
    assert exc.value.__cause__ is not None


@pytest.mark.parametrize("stage", ["planner", "executor"])
def test_refusal_is_rejected_even_with_valid_payload(install_response, stage):
    refusal = {
        "id": "msg_refusal",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "refusal", "refusal": "Cannot perform this refactoring."}],
    }
    valid_output = (
        text_item("{}") if stage == "planner" else function_item('{"files": []}')
    )
    install_response(response_with([valid_output, refusal]))

    with pytest.raises(RuntimeError, match=stage_error_prefix(stage)) as exc:
        call_stage(stage)

    assert "Cannot perform this refactoring." in str(exc.value)
    assert exc.value.__cause__ is not None


@pytest.mark.parametrize("stage", ["planner", "executor"])
def test_api_errors_preserve_the_original_cause(install_response, stage):
    error = openai.APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )
    install_response(error=error)

    with pytest.raises(RuntimeError, match=stage_error_prefix(stage)) as exc:
        call_stage(stage)

    assert exc.value.__cause__ is error


@pytest.mark.parametrize("stage", ["planner", "executor"])
def test_client_configuration_errors_have_stage_context(monkeypatch, stage):
    error = openai.OpenAIError("Missing API credentials")

    def missing_credentials():
        raise error

    monkeypatch.setattr(llm_clients.openai, "OpenAI", missing_credentials)

    with pytest.raises(RuntimeError, match=stage_error_prefix(stage)) as exc:
        call_stage(stage)

    assert exc.value.__cause__ is error


def call_stage(stage):
    if stage == "planner":
        return llm_clients.generate_refactor_plan("cyclic_dependency", "context")
    return llm_clients.execute_refactor({}, {}, {})


def stage_error_prefix(stage):
    if stage == "planner":
        return "^Failed to generate refactor plan:"
    return "^Failed to execute refactor:"
