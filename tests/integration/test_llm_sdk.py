"""Exercise the real OpenAI SDK through an in-memory HTTP transport."""

import json

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


@pytest.mark.parametrize("stage", ["planner", "executor"])
@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("https://api.openai.com/v1", id="openai"),
        pytest.param("http://localhost:11434/v1", id="ollama"),
    ],
)
def test_configured_requests_with_real_sdk(monkeypatch, stage, base_url):
    plan = {
        "strategy": "ExtractUtility",
        "explanation": "Extract shared behavior.",
        "actions": [
            {
                "operation": "move_func",
                "targets": ["a.py", "b.py"],
                "details": {"function": "helper", "options": [True, None, 1]},
            }
        ],
        "additional_decisions": {"groups": [["a", "b"]], "note": None},
    }
    output = (
        text_item(json.dumps(plan))
        if stage == "planner"
        else function_item('{"files": []}')
    )
    response = response_with([reasoning_item(), output])
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response.model_dump(mode="json"))

    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("REASONER_MODEL", "planner-model")
    monkeypatch.setenv("CODER_MODEL", "executor-model")
    monkeypatch.setenv("REASONER_REASONING_EFFORT", "high")
    monkeypatch.setenv("CODER_REASONING_EFFORT", "medium")
    # Mock only the HTTP boundary so the real SDK serializes and parses the request.
    with openai.OpenAI(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        monkeypatch.setattr(llm_clients.openai, "OpenAI", lambda: client)
        assert call_stage(stage) == (plan if stage == "planner" else [])
        prefix = "REASONER" if stage == "planner" else "CODER"
        monkeypatch.setenv(f"{prefix}_MODEL", "updated-model")
        monkeypatch.setenv(f"{prefix}_REASONING_EFFORT", "custom-effort")
        call_stage(stage)

    assert len(requests) == 2
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{base_url}/responses"
    body = json.loads(request.content)
    if stage == "planner":
        assert body["text"] == {
            "format": {
                "type": "json_schema",
                "name": "refactor_plan",
                "schema": {"type": "object", "additionalProperties": True},
                "strict": False,
            }
        }
        assert body["input"] == "context"
    assert body["model"] == f"{stage}-model"
    assert body["reasoning"] == {"effort": "high" if stage == "planner" else "medium"}
    assert body["store"] is False
    updated_body = json.loads(requests[1].content)
    assert updated_body["model"] == "updated-model"
    assert updated_body["reasoning"] == {"effort": "custom-effort"}


def call_stage(stage):
    if stage == "planner":
        return llm_clients.generate_refactor_plan("cyclic_dependency", "context")
    return llm_clients.execute_refactor({}, {}, {})
