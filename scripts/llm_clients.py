#!/usr/bin/env python3
import json
import os
import openai
from typing import Any, Dict, List

# ===== Configuration =====
# Defaults used when the corresponding environment variable is unset or empty.
REASONER_MODEL = "gpt-5.6-luna"   # reasoning‑oriented model
CODER_MODEL    = "gpt-5.6-luna"   # code‑generation model

# ===== Reasoner prompts =====
# Each prompt lists explicit refactoring options. The model MUST output valid JSON only
# (schema described inside each prompt) and MUST NOT reveal chain‑of‑thought.
REASONER_PROMPTS = {
    "cyclic_dependency": (
        "You are a reasoning‑focused software‑architecture assistant."\
        "\nTASK: Break the provided cyclic dependency with ONE of these strategies:"\
        "\n  • Interface        — introduce an interface / ABC to invert a dependency"\
        "\n  • MergeModules     — move / merge one class or file to eliminate the import edge"\
        "\n  • ObserverCallback — decouple via callback / event (publish‑subscribe)"\
        "\n  • ExtractUtility   — move shared logic to a neutral utility module"\
        "\n\nIMPORTANT: The prompt includes:"\
        "\n  - A dependency graph showing which modules import which"\
        "\n  - Files that import each participant (must be updated on deletion)"\
        "\n  Use the graph to identify which edge(s) to break."\
        "\nOUTPUT: JSON ONLY, schema:"\
        "\n  {\"strategy\",\"explanation\",\"actions\"}"\
        "\n    • strategy     ∈ {Interface,MergeModules,ObserverCallback,ExtractUtility}"\
        "\n    • explanation  – short human‑readable rationale (<=40 words)"\
        "\n    • actions      – array of objects, each with type, description, and rationale"\
        "\n      - type ∈ {create_interface,merge_module,move_func,modify_module}"\
        "\n      - description – short summary of the change, naming affected modules or symbols (<=40 words)"\
        "\n      - rationale – why this action helps resolve the smell (<=40 words)"\
        "\n      Keep description and rationale as plain-language strings; do not include source code, diffs, or full file contents."\
        "\nNO other text."
    ),

    "unneeded_abstraction": (
        "You are a reasoning‑focused software‑architecture assistant."\
        "\nTASK: Consolidate redundant abstractions with ONE main strategy:"\
        "\n  • CollapseHierarchy — keep canonical implementation, delete thin wrappers"\
        "\n  • MergeClasses      — merge partly‑unique duplicates into one class/module"\
        "\n  • ExtractUtility    — convert trivial wrapper to standalone helper function"\
        "\nOUTPUT: JSON ONLY, schema:"\
        "\n  {\"strategy\",\"canonical_module\",\"explanation\",\"actions\"}"\
        "\n    • strategy         ∈ {CollapseHierarchy,MergeClasses,ExtractUtility}"\
        "\n    • canonical_module – file to keep after consolidation"\
        "\n    • explanation      – short rationale (<=40 words)"\
        "\n    • actions          – array of objects, each with type, description, and rationale"\
        "\n      - type ∈ {remove_module,merge_module,modify_module}"\
        "\n      - description – short summary of the change, naming affected modules or symbols (<=40 words)"\
        "\n      - rationale – why this action helps resolve the smell (<=40 words)"\
        "\n      Keep description and rationale as plain-language strings; do not include source code, diffs, or full file contents."\
        "\nNO other text."
    )
}

# ===== Structured‑output schema for implementation model =====
IMPLEMENTATION_TOOL_SCHEMA = [
    {
        "type": "function",
        "name": "submit_refactored_files",
        "description": "Return the refactored file set with operation types",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "The path to the file",
                            },
                            "operation": {
                                "type": "string",
                                "enum": ["create", "modify", "delete"],
                                "description": "The operation type to perform on the file",
                            },
                            "content": {
                                "type": ["string", "null"],
                                "description": "The content of the file (null for delete operations)",
                            },
                        },
                        "required": ["path", "operation", "content"],
                    },
                }
            },
            "required": ["files"],
        },
    }
]


def _validate_response(response: Any) -> None:
    """Reject failed, partial, or refused output before either stage parses it."""
    if response.status != "completed":
        details = getattr(response, "incomplete_details", None)
        error = getattr(response, "error", None)
        reason = getattr(details, "reason", None) or getattr(error, "message", None)
        message = f"Response status is {response.status!r}"
        if reason:
            message += f": {reason}"
        raise ValueError(message)

    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "refusal":
                    raise ValueError(f"Model refused the request: {content.refusal}")


def _parse_json_object(text: str, description: str) -> Dict:
    """Validate only the outer JSON contract, preserving all nested values."""
    if not text or not text.strip():
        raise ValueError(f"Empty {description}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"{description.capitalize()} must be a JSON object")
    return payload


def generate_refactor_plan(smell_type: str, prompt_context: str) -> Dict:
    """Call the reasoning model to plan the refactor. Returns a JSON dict matching the schema
    defined in the corresponding REASONER_PROMPTS entry.
    
    Args:
        smell_type (str): Type of smell to address (must be a key in REASONER_PROMPTS)
        prompt_context (str): Context about the code smell for the model
        
    Returns:
        Dict: A dictionary containing the refactoring plan
    """
    if smell_type not in REASONER_PROMPTS:
        raise ValueError(
            f"Unknown smell type: {smell_type}. "
            f"Must be one of: {list(REASONER_PROMPTS.keys())}"
        )

    try:
        client = openai.OpenAI()
        resp = client.responses.create(
            model=os.getenv("REASONER_MODEL") or REASONER_MODEL,
            instructions=REASONER_PROMPTS[smell_type],
            input=prompt_context,
            # Ollama's Responses adapter applies json_schema, but not json_object.
            # Keep the schema open to preserve arbitrary planning decisions.
            text={
                "format": {
                    "type": "json_schema",
                    "name": "refactor_plan",
                    "schema": {"type": "object", "additionalProperties": True},
                    "strict": False,
                }
            },
            reasoning={"effort": os.getenv("REASONER_REASONING_EFFORT") or "xhigh"},
            store=False,
        )
        _validate_response(resp)
        return _parse_json_object(resp.output_text, "refactor plan")
    except Exception as e:
        raise RuntimeError(f"Failed to generate refactor plan: {str(e)}") from e


def execute_refactor(
    plan: Dict[str, Any],
    modules: Dict[str, str],
    importers_map: Dict[str, List[str]],
) -> List[Dict]:
    """Ask the code generation model to apply the complete plan and return refactored files.

    Args:
        plan (Dict[str, Any]): Complete plan loaded from proposal.json, including
            planning decisions and application-added metadata
        modules (Dict[str, str]): Dictionary mapping filenames to their content
        importers_map (Dict[str, List[str]]): Map of participant -> importers (only those with content in modules)

    Returns:
        List[Dict]: List of file operations, each containing:
            - path: The file path
            - operation: The operation type (create/modify/delete)
            - content: The file content (None for delete operations)
    """
    exec_prompt = json.dumps(
        {
            # Similarity metadata is only for the correctness filter.
            "plan": {key: value for key, value in plan.items() if key != "similarity_context"},
            "modules": modules,
            "importers_by_participant": importers_map,
        },
        indent=2,
    )

    system_prompt = """You are a precise code-transformation assistant. Implement the supplied refactoring plan.

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

    try:
        client = openai.OpenAI()
        resp = client.responses.create(
            model=os.getenv("CODER_MODEL") or CODER_MODEL,
            instructions=system_prompt,
            input=exec_prompt,
            reasoning={"effort": os.getenv("CODER_REASONING_EFFORT") or "low"},
            store=False,
            tools=IMPLEMENTATION_TOOL_SCHEMA,
            tool_choice={"type": "function", "name": "submit_refactored_files"},
            parallel_tool_calls=False,
        )
        _validate_response(resp)
        function_calls = [item for item in resp.output if item.type == "function_call"]
        if len(function_calls) != 1:
            raise ValueError(
                f"Expected exactly one function call, received {len(function_calls)}"
            )
        tool_call = function_calls[0]
        if tool_call.name != "submit_refactored_files":
            raise ValueError(f"Unexpected function call: {tool_call.name}")
        payload = _parse_json_object(tool_call.arguments, "function arguments")
        file_operations = payload.get("files")
        if not isinstance(file_operations, list) or any(
            not isinstance(file_op, dict) for file_op in file_operations
        ):
            raise ValueError("Function arguments must contain a 'files' list of objects")
        return file_operations
    except Exception as e:
        raise RuntimeError(f"Failed to execute refactor: {str(e)}") from e
