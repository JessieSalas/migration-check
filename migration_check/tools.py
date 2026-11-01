"""Tool definitions and the deliberately small JSON-schema argument checker.

Checks cover required properties, pattern properties, boolean subschemas,
nested objects/arrays (including tuple items) and basic JSON types.
This is not a full JSON Schema implementation (for example, $ref is not resolved).
"""

import hashlib
import json
import math
from pathlib import Path
import re

TOOL_SYSTEM = "Continue the conversation. Use the available tools when appropriate."
STOP_HAZARDS = ("refusal", "truncated", "filtered")
TOOL_HAZARDS = ("invalid_arguments", "multiple_tool_calls", "unknown_tool", "empty_replies") + STOP_HAZARDS
REPLY_FAILURES = STOP_HAZARDS + ("unknown_tool", "unparseable")


def stop_hazards(response):
    return {"refusal": response.get("stop_reason") == "refusal" or bool(response.get("refusal")),
            "truncated": response.get("stop_reason") == "max_tokens" or response.get("finish_reason") == "length",
            "filtered": response.get("finish_reason") == "content_filter"}


def read_tools(path):
    try:
        definitions = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in tools file {path}: {exc.msg}, line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(definitions, list) or not definitions:
        raise ValueError(f"Tools file {path}: tools must be a nonempty list of function definitions")
    tools, seen = [], set()
    for index, definition in enumerate(definitions):
        name = None
        try:
            if not isinstance(definition, dict) or definition.get("type", "function") != "function":
                raise ValueError("Each tool must be a function definition")
            function = definition.get("function", definition)
            if not isinstance(function, dict):
                raise ValueError("Each tool needs name, description and parameters")
            name = function.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("Tool names must match ^[A-Za-z0-9_-]{1,64}$")
            if name == "none" or name in seen:
                raise ValueError("Tool names must be unique and cannot be the reserved name 'none'")
            if "input_schema" in function:
                raise ValueError("--tools takes the OpenAI function format: use parameters, not input_schema")
            schema = function.get("parameters")
            if not isinstance(function.get("description"), str) or not isinstance(schema, dict):
                raise ValueError("Each tool needs a string description and parameters JSON schema")
            if schema.get("type") != "object":
                raise ValueError("parameters.type must be 'object'")
            if "strict" in function and not isinstance(function["strict"], bool):
                raise ValueError("strict must be a boolean")
            check_schema(schema)
        except ValueError as exc:
            raise ValueError(f"Tools file {path}, entry {index} (name {name!r}): {exc}") from exc
        seen.add(name)
        tools.append({"type": "function", "function": {key: function[key] for key in
                      ("name", "description", "parameters", "strict") if key in function}})
    return tools


def check_schema(schema, path="parameters"):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: tool parameter schemas must be objects or booleans")
    kind = schema.get("type")
    types = kind if isinstance(kind, list) else [kind] if kind is not None else []
    if kind == [] or any(t not in ("object", "array", "string", "number", "integer", "boolean", "null") for t in types):
        raise ValueError(f"{path}: unsupported basic JSON type in tool parameters")
    properties, required = schema.get("properties", {}), schema.get("required", [])
    if (not isinstance(properties, dict) or not isinstance(required, list) or
            any(not isinstance(key, str) for key in required)):
        raise ValueError(f"{path}: tool parameters need object properties and a list of required keys")
    for key, child in properties.items():
        check_schema(child, f"{path}.properties.{key}")
    patterns = schema.get("patternProperties", {})
    if not isinstance(patterns, dict):
        raise ValueError(f"{path}.patternProperties must be an object")
    for pattern, child in patterns.items():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{path}.patternProperties.{pattern}: invalid pattern") from exc
        check_schema(child, f"{path}.patternProperties.{pattern}")
    if "additionalProperties" in schema:
        check_schema(schema["additionalProperties"], f"{path}.additionalProperties")
    if "items" in schema:
        if isinstance(schema["items"], list):
            for index, child in enumerate(schema["items"]):
                check_schema(child, f"{path}.items[{index}]")
        else:
            check_schema(schema["items"], f"{path}.items")


def schema_valid(value, schema):
    if isinstance(schema, bool):
        return schema
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": isinstance(value, bool), "null": value is None,
               "number": type(value) in (int, float) and (not isinstance(value, float) or math.isfinite(value)),
               "integer": type(value) in (int, float) and
               (isinstance(value, int) or (math.isfinite(value) and value.is_integer()))}
    kind = schema.get("type")
    if kind is not None and not any(matches.get(t, False) for t in (kind if isinstance(kind, list) else [kind])):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        for key, child in value.items():
            applicable = [subschema for pattern, subschema in patterns.items() if re.search(pattern, key)]
            if key in properties:
                applicable.append(properties[key])
            if not applicable:
                applicable.append(schema.get("additionalProperties", True))
            if not all(schema_valid(child, subschema) for subschema in applicable):
                return False
        return True
    if isinstance(value, list) and "items" in schema:
        if isinstance(schema["items"], list):
            return all(schema_valid(v, child) for v, child in zip(value, schema["items"]))
        return all(schema_valid(v, schema["items"]) for v in value)
    return True


def arguments_valid(raw, schema):
    if not isinstance(raw, str) or schema is None:
        return False
    try:
        value = json.loads(raw, parse_constant=reject_constant)
        return isinstance(value, dict) and schema_valid(value, schema)
    except (ValueError, TypeError, RecursionError):
        return False


def reject_constant(value):
    raise ValueError("Not a JSON constant: " + value)


def parse_tool_reply(response, tools):
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    stopped = stop_hazards(response)
    failed = any(stopped.values())
    calls = []
    for call in response.get("tool_calls", []):
        name, arguments = call.get("name"), call.get("arguments")
        name = name if isinstance(name, str) else None
        calls.append({"name": name, "arguments": arguments,
                      "args_valid": None if failed else arguments_valid(arguments, schemas.get(name))})
    text = response.get("text") or ""
    empty = not failed and not calls and not text.strip()
    answer = calls[0]["name"] if calls else None if empty else "none"
    unknown = not failed and any(c["name"] not in schemas for c in calls)
    if failed or (calls and calls[0]["name"] not in schemas) or not isinstance(answer, str) or not answer:
        answer = None
    return {"answer": answer, "tool_calls": calls, "n_tool_calls": len(calls),
            "args_valid": all(c["args_valid"] for c in calls) if calls and not failed else None,
            "invalid_arguments": not failed and any(c["name"] in schemas and not c["args_valid"] for c in calls),
            "multiple_tool_calls": not failed and len(calls) > 1,
            "unknown_tool": unknown, "empty_replies": empty, **stopped}


def sample_arguments(schema):
    """Build basic fixture arguments, never from gold or production values."""
    if isinstance(schema, bool):
        return None
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        return {key: sample_arguments(child) for key, child in schema.get("properties", {}).items()}
    return {"array": [], "string": "fictional", "integer": 1, "number": 1.0,
            "boolean": True, "null": None}.get(kind)


def mock_tool(model, context):
    tools = context["tools"]
    if model not in ("correct", "wrong_tool", "none", "bad_args", "flaky"):
        raise ValueError("Tool mock modes: correct, wrong_tool, none, bad_args, flaky")
    text = context.get("input", "").casefold()
    words = set(re.findall(r"\w+", text))
    scores = [len(set(re.findall(r"\w+", t["function"]["name"].casefold().replace("_", " "))) & words)
              for t in tools]
    selected = max(range(len(tools)), key=lambda i: scores[i])
    if model == "flaky":
        key = json.dumps([context.get("seed", 0), context.get("arm"), context.get("item_id"),
                          context["draw"], text], sort_keys=True)
        model = ("correct", "wrong_tool", "none", "bad_args")[hashlib.sha256(key.encode()).digest()[0] % 4]
    if model == "none":
        return {"text": "Fictional support reply.", "tool_calls": []}
    if model == "correct" and not any(scores):
        return {"text": "Fictional support reply.", "tool_calls": []}
    if model == "wrong_tool":
        selected = (selected + 1) % len(tools) if any(scores) else 0
        if len(tools) == 1 and any(scores):
            return {"text": "Fictional support reply.", "tool_calls": []}
    function = tools[selected]["function"]
    arguments = "{bad JSON" if model == "bad_args" else json.dumps(sample_arguments(function["parameters"]))
    return {"text": "", "tool_calls": [{"name": function["name"], "arguments": arguments}]}
