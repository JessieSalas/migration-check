"""Offline regressions for FIX-v02.md and the three review lenses."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import test_migration_check as base
import test_tools as tool_base
from migration_check import providers
from migration_check.__main__ import main, parser
from migration_check.analysis import analyze, load_run
from migration_check.protocol import build_request, read_inputs
from migration_check.report import build_report
from migration_check.runner import run
from migration_check.tools import arguments_valid, parse_tool_reply, read_tools

ROOT = Path(__file__).resolve().parents[1]


class V02FixTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp
    make_run = tool_base.ToolTests.make_run

    def test_stop_envelopes_are_failed_draws_with_recorded_metadata(self):
        tools = read_tools(ROOT / "examples/tools.json")
        states = (("anthropic", "refusal"), ("anthropic", "truncated"),
                  ("openai", "refusal"), ("openai", "truncated"), ("openai", "filtered"),
                  ("openai-compatible", "refusal"), ("openai-compatible", "truncated"),
                  ("openai-compatible", "filtered"))
        for provider, state in states:
            with self.subTest(provider=provider, state=state):
                fixture = "anthropic" if provider == "anthropic" else "openai"
                stub = json.loads((ROOT / "tests/fixtures" / f"{fixture}_{state}.json").read_text())
                with patch.dict(os.environ, {"OPENAI_API_KEY": "stub", "ANTHROPIC_API_KEY": "stub",
                                             "MIGRATION_CHECK_BASE_URL": "http://localhost/v1"}), \
                        patch.object(providers, "post", return_value=stub):
                    response = providers.call(provider + ":stub", "Support", [], {"answer": "tool", "tools": tools})
                parsed = parse_tool_reply(response, tools)
                self.assertIsNone(parsed["answer"])
                self.assertTrue(parsed[state])
                self.assertIsNone(parsed["args_valid"])
                self.assertFalse(parsed["invalid_arguments"])
                self.assertFalse(parsed["empty_replies"])
                with patch.object(providers, "call", return_value=response):
                    directory, manifest, rows, calls = self.make_run(draws=2, name=provider + state)
                data = analyze(manifest, rows, calls)
                self.assertTrue(manifest["complete"])
                self.assertEqual(data["gold"]["new_accuracy"], 0)
                for call in calls:
                    self.assertEqual(call["retries"], 0)
                    self.assertIsNone(call["predictions"][0]["answer"])
                    attempt = call["attempts"][0]
                    self.assertEqual(attempt["error"], state)
                    for key in ("stop_reason", "finish_reason"):
                        self.assertEqual(attempt[key], response.get(key))
                    self.assertEqual(attempt["message_refusal"], response.get("refusal"))
                for arm in ("old", "new"):
                    self.assertEqual(data["hazard_summary"][arm][state], 6)
                    self.assertEqual(data["hazard_summary"][arm]["invalid_arguments"], 0)
                    self.assertTrue(all(draw[state] == 3 for draw in data["draw_strip"][arm]))
                output = self.root / f"{provider}_{state}.html"
                build_report(directory, output)
                decision = json.loads(output.with_suffix(".decision.json").read_text())
                self.assertEqual(decision["tool_choice_hazards"]["new"][state], 6)

    def test_success_stop_reasons_and_text_mode_failures(self):
        tools = read_tools(ROOT / "examples/tools.json")
        for stops in ({"stop_reason": "end_turn"}, {"finish_reason": "stop"}):
            parsed = parse_tool_reply({"text": "Hello", **stops}, tools)
            self.assertEqual(parsed["answer"], "none")
            self.assertFalse(any(parsed[key] for key in ("refusal", "truncated", "filtered")))
        response = {"text": "yes", "served_models": ["old"], "error": None, "finish_reason": "length"}
        with patch.object(providers, "call", return_value=response):
            _, manifest, rows, calls = base.OfflineTests.make_run(self, draws=2, labels=["yes", "no"])
        data = analyze(manifest, rows, calls)
        self.assertEqual(data["gold"]["old_accuracy"], 0)
        self.assertEqual(data["hazard_summary"]["old"]["truncated"], 4)
        self.assertTrue(all(c["attempts"][0]["finish_reason"] == "length" for c in calls))

    def test_message_keys_are_normalized_and_blank_content_is_line_numbered(self):
        row = {"id": "chat", "messages": [{"role": "user", "content": "Hi", "name": "customer", "timestamp": "t"}]}
        path = self.root / "input.jsonl"
        path.write_text(json.dumps(row))
        rows = read_inputs(path)
        _, messages = build_request(rows, [])
        self.assertEqual(messages, [{"role": "user", "content": "Hi"}])
        for blank in ("", " \t\n"):
            for role in ("system", "user", "assistant"):
                row["messages"] = [{"role": role, "content": blank}, {"role": "user", "content": "Hi"}]
                path.write_text("\n" + json.dumps(row))
                with self.assertRaisesRegex(ValueError, "Input line 2: message content must not be empty"):
                    read_inputs(path)
            path.write_text("\n" + json.dumps({"id": "plain", "input": blank}))
            with self.assertRaisesRegex(ValueError, "Input line 2: input must be a nonempty string"):
                read_inputs(path)

    def test_assistant_final_turn_rejected_before_calls_in_both_modes(self):
        path = self.root / "input.jsonl"
        labels = self.root / "labels.txt"
        labels.write_text("yes\nno\n")
        for trailing_system in (False, True):
            messages = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Let me"}]
            if trailing_system:
                messages.append({"role": "system", "content": "Support"})
            path.write_text("\n" + json.dumps({"id": "chat", "messages": messages}))
            for mode_args in (["--answer", "tool", "--tools", str(ROOT / "examples/tools.json")],
                              ["--labels", str(labels)]):
                args = parser().parse_args(["run", "--inputs", str(path), "--old", "mock:correct",
                                           "--new", "mock:correct", "--out", str(self.root / "rejected"), *mode_args])
                with patch.object(providers, "call") as call:
                    with self.assertRaisesRegex(ValueError, "Input line 2: the last message must be a user turn"):
                        run(args)
                call.assert_not_called()
                self.assertFalse((self.root / "rejected").exists())

    def test_tool_definitions_normalize_exact_payload_and_report_file_entry_path(self):
        path = self.root / "tools.json"
        canonical = read_tools(ROOT / "examples/tools.json")
        function = {**canonical[0]["function"], "strict": False}
        for definition in ({"type": "function", **function, "bogus": 1},
                           {"type": "function", "function": {**function, "bogus": 1}}):
            path.write_text(json.dumps([definition]))
            tools = read_tools(path)
            self.assertEqual(tools, [{"type": "function", "function": function}])
            stub = json.loads((ROOT / "tests/fixtures/openai_tools.json").read_text())
            with patch.dict(os.environ, {"OPENAI_API_KEY": "stub"}), patch.object(providers, "post", return_value=stub) as post:
                providers.call("openai:stub", "Support", [], {"answer": "tool", "tools": tools})
            self.assertEqual(post.call_args.args[1]["tools"], tools)
        invalid = [{**function, "name": name} for name in ("", "bad name", "a.b", "a" * 65, "none", "t\n")]
        invalid += [{**function, "parameters": schema} for schema in ({}, {"type": "array"}, True, [])]
        invalid += [{**function, "strict": "yes"}, {"name": "t", "description": "d", "input_schema": {"type": "object"}},
                    {**function, "parameters": {"type": "object", "properties": {"meta": {"type": "bogus"}}}}]
        for definition in invalid:
            path.write_text(json.dumps([canonical[1], definition]))
            with self.subTest(definition=definition), self.assertRaises(ValueError) as caught:
                read_tools(path)
            self.assertIn(str(path), str(caught.exception))
            self.assertIn("entry 1", str(caught.exception))
        self.assertIn("parameters.properties.meta", str(caught.exception))
        path.write_text("not json")
        with self.assertRaisesRegex(ValueError, "Invalid JSON in tools file .*tools.json.*line 1 column 1"):
            read_tools(path)

    def test_unknown_none_tool_is_never_a_correct_text_fallback(self):
        tools = read_tools(ROOT / "examples/tools.json")
        for index, name in enumerate(("none", "made_up", None)):
            response = {"text": "hello", "tool_calls": [{"name": name, "arguments": "{}"}],
                        "served_models": ["correct"], "error": None}
            parsed = parse_tool_reply(response, tools)
            self.assertIsNone(parsed["answer"])
            self.assertTrue(parsed["unknown_tool"])
            with patch.object(providers, "call", return_value=response):
                _, manifest, rows, calls = self.make_run(draws=2, name=f"unknown_{index}")
            data = analyze(manifest, rows, calls)
            self.assertEqual(data["gold"]["new_accuracy"], 0)
            self.assertEqual(data["hazard_summary"]["new"]["unknown_tool"], 6)
            self.assertEqual(data["tool_confusion"], [{"old_mode": None, "new_mode": None, "count": 3}])
            for row in rows:
                row.pop("gold")
            self.assertIsNone(analyze(manifest, rows, calls)["unlabelled"]["items"][0]["new_mode"])
        def reply(spec, *args, **kwargs):
            return {"text": "Text fallback" if spec == "mock:correct" else "",
                    "tool_calls": [] if spec == "mock:correct" else [{"name": "none", "arguments": "{}"}],
                    "served_models": [spec.split(":")[1]], "error": None}
        with patch.object(providers, "call", side_effect=reply):
            _, manifest, rows, calls = self.make_run(draws=5, name="fallback_vs_unknown")
        data = analyze(manifest, rows, calls)
        self.assertEqual(data["gold"]["new_accuracy"], 0)
        self.assertEqual(data["tool_confusion"], [{"old_mode": "none", "new_mode": None, "count": 3}])
        for row in rows:
            row.pop("gold")
        self.assertTrue(all(item["changed"] for item in analyze(manifest, rows, calls)["unlabelled"]["items"]))

    def test_provider_error_echoes_are_absent_from_capture_and_all_exports(self):
        tools = read_tools(ROOT / "examples/tools.json")
        detail = "tools.0 input_value=" + json.dumps(tools[0])[:160] + ' args={"secret":"PRIVATE_ARGUMENT"}'
        opener = MagicMock()

        def fail(*args, **kwargs):
            body = json.dumps({"error": {"message": detail}}).encode()
            raise urllib.error.HTTPError("https://example.invalid", 400, "Bad Request", {}, io.BytesIO(body))

        opener.open.side_effect = fail
        directory = self.root / "private"
        stream = io.StringIO()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "stub"}), \
                patch.object(providers.urllib.request, "build_opener", return_value=opener), \
                redirect_stdout(stream), redirect_stderr(stream):
            rc = main(["run", "--inputs", str(ROOT / "examples/tool_inputs.jsonl"), "--tools", str(ROOT / "examples/tools.json"),
                       "--answer", "tool", "--old", "openai:stub", "--new", "mock:correct", "--retries", "0",
                       "--out", str(directory)])
        self.assertEqual(rc, 2)
        self.assertIn("provider HTTP 400", stream.getvalue())
        manifest, rows, calls = load_run(directory)
        errors = [a["error"] for c in calls if c["arm"] == "old" for a in c["attempts"]]
        self.assertEqual(errors, ["provider HTTP 400"])
        output = self.root / "private.html"
        build_report(directory, output)
        texts = [stream.getvalue(), (directory / "calls.jsonl").read_text()]
        texts += [output.with_suffix(suffix).read_text() for suffix in (".html", ".analysis.json", ".decision.json", ".decision.md")]
        for text in texts:
            self.assertNotIn(tools[0]["function"]["description"], text)
            self.assertNotIn("PRIVATE_ARGUMENT", text)
            self.assertNotIn("input_value", text)
        # Old captures or an adapter returning its own error get the same safe summary.
        old = next(c for c in calls if c["arm"] == "old")
        old["attempts"][0]["error"] = "provider HTTP 400: " + detail
        self.assertNotIn("input_value", json.dumps(analyze(manifest, rows, calls)))
        self.assertIn("input_value", json.dumps(analyze(manifest, rows, calls, show_text=True)))
        response = {"text": "", "served_models": ["correct"], "error": "provider HTTP 400: " + detail}
        with patch.object(providers, "call", return_value=response):
            with self.assertRaisesRegex(ValueError, "Stopped early") as caught:
                self.make_run(name="returned_error", draws=1)
        self.assertNotIn("input_value", str(caught.exception))
        self.assertNotIn("input_value", (self.root / "returned_error/calls.jsonl").read_text())

    def test_boolean_tuple_and_pattern_property_schemas(self):
        schema = {"type": "object", "properties": {"meta": True, "blocked": False,
                  "pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}},
                  "patternProperties": {"^x_": {"type": "string"}}, "additionalProperties": False}
        path = self.root / "schema.json"
        path.write_text(json.dumps([{"name": "t", "description": "d", "parameters": schema}]))
        read_tools(path)
        for value in ({"meta": [1, None]}, {"pair": ["a", 1]}, {"x_a": "v"}):
            self.assertTrue(arguments_valid(json.dumps(value), schema), value)
        for value in ({"blocked": None}, {"pair": [1, "a"]}, {"x_a": 3}, {"other": "v"}):
            self.assertFalse(arguments_valid(json.dumps(value), schema), value)
        # Even a schema without type cannot allow non-object function arguments.
        for raw in ("[]", "1", '"a"', "null"):
            self.assertFalse(arguments_valid(raw, {}))
        schema["properties"]["x_a"] = False
        self.assertFalse(arguments_valid('{"x_a":"v"}', schema))
        schema["patternProperties"] = {"[": True}
        path.write_text(json.dumps([{"name": "t", "description": "d", "parameters": schema}]))
        with self.assertRaisesRegex(ValueError, "entry 0.*patternProperties.*invalid pattern"):
            read_tools(path)

    def test_mock_correct_scores_contents_without_role_or_json_words(self):
        tools = self.root / "role_tools.json"
        tools.write_text(json.dumps([{"name": name, "description": "d", "parameters": {"type": "object"}}
                                    for name in ("user_profile", "update_content", "assistant_system", "role")]))
        inputs = self.root / "role_inputs.jsonl"
        inputs.write_text(json.dumps({"id": "fallback", "messages": [{"role": "system", "content": "Support"},
                                      {"role": "user", "content": "What hours is this fictional shop open?"}], "gold": "none"}))
        args = parser().parse_args(["run", "--inputs", str(inputs), "--tools", str(tools), "--answer", "tool",
                                   "--old", "mock:correct", "--new", "mock:correct", "--draws", "1", "--out", str(self.root / "roles")])
        result = run(args)
        manifest, rows, calls = load_run(result["directory"])
        self.assertTrue(all(call["predictions"][0]["answer"] == "none" for call in calls))
        self.assertEqual(analyze(manifest, rows, calls)["gold"]["new_accuracy"], 1)


if __name__ == "__main__":
    unittest.main()
