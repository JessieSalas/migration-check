"""Tool-choice v0.2 coverage. All provider responses are offline stubs."""

import json
import os
from pathlib import Path
import random
import unittest
from unittest.mock import patch

import test_migration_check as base
from migration_check import providers
from migration_check.__main__ import parser
from migration_check.analysis import analyze, load_run
from migration_check.report import build_report
from migration_check.runner import run
from migration_check.tools import (TOOL_HAZARDS, TOOL_SYSTEM, arguments_valid, mock_tool,
                                   parse_tool_reply, read_tools)

ROOT = Path(__file__).resolve().parents[1]


class ToolTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp

    def make_run(self, gold=True, new="wrong_tool", old="correct", name="run", draws=10, extra=()):
        rows = [{"id": "status", "messages": [{"role": "system", "content": "Fictional support."},
                 {"role": "user", "content": "Check order status for DEMO."}], "slice": "status"},
                {"id": "refund", "messages": [{"role": "user", "content": "Open a refund request."}],
                 "slice": "refund"},
                {"id": "fallback", "input": "Hello there!", "slice": "fallback"}]
        if gold:
            for row, expected in zip(rows, ("order_status", "refund_request", "none")):
                row["gold"] = expected
        inputs = self.root / (name + ".jsonl")
        inputs.write_text("\n".join(json.dumps(row) for row in rows))
        args = parser().parse_args(["run", "--inputs", str(inputs), "--tools", str(ROOT / "examples/tools.json"),
                                   "--answer", "tool", "--old", "mock:" + old, "--new", "mock:" + new,
                                   "--draws", str(draws), "--out", str(self.root / name), *extra])
        result = run(args)
        manifest, rows, calls = load_run(result["directory"])
        return Path(result["directory"]), manifest, rows, calls

    def test_gold_end_to_end_choice_accuracy_confusion_and_decision(self):
        directory, manifest, rows, calls = self.make_run()
        self.assertEqual(len(calls), 60)
        self.assertEqual(manifest["system"], TOOL_SYSTEM)
        data = analyze(manifest, rows, calls)
        self.assertEqual(data["gold"]["old_accuracy"], 1)
        self.assertEqual(data["gold"]["new_accuracy"], 0)
        self.assertEqual(data["gold"]["Dstar"], -1)
        self.assertEqual(len(data["gold"]["primary_min_p"]["slice_tests"]), 3)
        self.assertEqual([q["kind"] for q in data["review_queue"]], ["stable_regression"] * 3)
        self.assertEqual(sum(row["count"] for row in data["tool_confusion"]), 3)
        self.assertTrue(all(d["value"] == 1 for d in data["draw_strip"]["old"]))
        output = self.root / "report.html"
        build_report(directory, output)
        exported = json.loads(output.with_suffix(".analysis.json").read_text())
        self.assertEqual(exported["tool_confusion"], data["tool_confusion"])
        markdown = output.with_suffix(".decision.md").read_text()
        self.assertLess(markdown.index("Tool-choice hazards"), markdown.index("## Item"))
        self.assertIn("invalid arguments: 0", markdown)
        self.assertEqual(exported["decision_record"]["tool_choice_hazards"]["old"], dict.fromkeys(TOOL_HAZARDS, 0))

    def test_without_gold_and_mixed_gold_use_existing_tests(self):
        directory, manifest, rows, calls = self.make_run(gold=False)
        data = analyze(manifest, rows, calls)
        self.assertIsNone(data["gold"])
        self.assertEqual(len(data["review_queue"]), 3)
        self.assertTrue(all(item["changed"] and item["adjusted_p"] <= .05 for item in data["unlabelled"]["items"]))
        self.assertTrue(all(d["value"] == 0 for d in data["draw_strip"]["old"]))
        output = self.root / "no-gold.html"
        build_report(directory, output)
        exported = json.loads(output.with_suffix(".analysis.json").read_text())
        self.assertEqual(exported["tool_confusion"], data["tool_confusion"])
        self.assertEqual(len(exported["review_queue"]), 3)
        rows[0]["gold"] = "order_status"
        mixed = analyze(manifest, rows, calls)
        self.assertEqual(mixed["gold"]["n_items"], 1)
        self.assertEqual(mixed["unlabelled"]["n_items"], 2)
        self.assertEqual(len(mixed["tool_confusion"]), 3)

    def test_bad_arguments_are_independent_of_choice_accuracy(self):
        _, manifest, rows, calls = self.make_run(new="bad_args")
        data = analyze(manifest, rows, calls)
        self.assertEqual(data["hazard_summary"]["new"]["invalid_arguments"], 30)
        self.assertEqual(data["hazard_summary"]["new"]["dominant_pattern"], "invalid_arguments")
        self.assertTrue(all(d["invalid_arguments"] == 3 for d in data["draw_strip"]["new"]))
        self.assertEqual(data["gold"]["new_accuracy"], 2 / 3)
        self.assertTrue(all(c["args_valid"] is False and c["n_tool_calls"] == 1 for c in calls if c["arm"] == "new"))

    def test_required_keys_nested_types_arrays_and_strict_json(self):
        schema = {"type": "object", "required": ["code", "count", "nested", "tags"],
                  "properties": {"code": {"type": "string"}, "count": {"type": "integer"},
                                 "nested": {"type": "object", "required": ["enabled"],
                                            "properties": {"enabled": {"type": "boolean"}}},
                                 "tags": {"type": "array", "items": {"type": ["string", "null"]}}},
                  "additionalProperties": False}
        good = {"code": "DEMO", "count": 1, "nested": {"enabled": True}, "tags": ["a", None]}
        self.assertTrue(arguments_valid(json.dumps(good), schema))
        self.assertTrue(arguments_valid(json.dumps({**good, "count": 1.0}), schema))
        for value in ({**good, "count": True}, {**good, "count": 1.2}, {**good, "code": None},
                      {**good, "nested": {}}, {**good, "tags": [1]}, {**good, "extra": 1}, [], {}, None):
            self.assertFalse(arguments_valid(json.dumps(value), schema), value)
        for raw in ("{bad", "", '{"count": NaN}', "Infinity", None):
            self.assertFalse(arguments_valid(raw, schema))
        self.assertFalse(arguments_valid('{"x": NaN}', {"type": "object"}))
        self.assertFalse(arguments_valid('{"x": 1e999}', {"type": "object", "properties": {"x": {"type": "number"}}}))

    def test_hazards_inspect_all_calls_empty_replies_and_text_fallback(self):
        tools = read_tools(ROOT / "examples/tools.json")
        parsed = parse_tool_reply({"text": "Explanation", "tool_calls": [
            {"name": "order_status", "arguments": '{"order_code":"DEMO"}'},
            {"name": "unknown", "arguments": "{}"}]}, tools)
        self.assertEqual(parsed["answer"], "order_status")
        self.assertEqual(parsed["n_tool_calls"], 2)
        self.assertTrue(parsed["multiple_tool_calls"] and parsed["unknown_tool"])
        self.assertFalse(parsed["invalid_arguments"])
        self.assertTrue(parsed["tool_calls"][0]["args_valid"])
        self.assertFalse(parsed["args_valid"])
        self.assertEqual(parse_tool_reply({"text": "hello"}, tools)["answer"], "none")
        self.assertIsNone(parse_tool_reply({"text": " \n"}, tools)["answer"])
        self.assertTrue(parse_tool_reply({"text": ""}, tools)["empty_replies"])
        self.assertTrue(parse_tool_reply({"tool_calls": [{"name": None}]}, tools)["unknown_tool"])
        response = {"text": "", "tool_calls": parsed["tool_calls"], "served_models": ["correct"], "error": None}
        with patch.object(providers, "call", return_value=response):
            _, m, rows, calls = self.make_run(draws=2)
        data = analyze(m, rows, calls)
        for arm in ("old", "new"):
            self.assertEqual(data["hazard_summary"][arm]["invalid_arguments"], 0)
            for key in ("multiple_tool_calls", "unknown_tool"):
                self.assertEqual(data["hazard_summary"][arm][key], 6)
        response["tool_calls"] = []
        with patch.object(providers, "call", return_value=response):
            _, m, rows, calls = self.make_run(draws=2, name="empty")
        data = analyze(m, rows, calls)
        self.assertEqual(data["hazard_summary"]["new"]["empty_replies"], 6)
        self.assertEqual(data["hazard_summary"]["new"]["parse_failures"], 6)
        self.assertTrue(all(c["retries"] == 2 for c in calls))

    def test_raw_arguments_never_export_even_with_show_text(self):
        response = {"text": "", "tool_calls": [{"name": "order_status", "arguments": '{"order_code":"PRIVATE_ARGUMENT"}'}],
                    "served_models": ["correct"], "error": None}
        with patch.object(providers, "call", return_value=response):
            directory, _, _, _ = self.make_run(draws=1)
        self.assertIn("PRIVATE_ARGUMENT", (directory / "calls.jsonl").read_text())
        for show_text in (False, True):
            output = self.root / (str(show_text) + ".html")
            build_report(directory, output, show_text=show_text)
            for suffix in (".html", ".analysis.json", ".decision.json", ".decision.md"):
                self.assertNotIn("PRIVATE_ARGUMENT", output.with_suffix(suffix).read_text())

    def test_validation_rejects_batch_cli_missing_tools_and_bad_gold(self):
        for extra, message in ((("--batch", "2"), "per conversation"),
                               (("--new", "claude-cli:m"), "does not support tool mode"),
                               (("--answer", "text"), "requires --answer tool")):
            with self.assertRaisesRegex(ValueError, message):
                self.make_run(extra=extra)
        with self.assertRaisesRegex(ValueError, "requires --tools"):
            self.make_run(extra=("--tools", ""))
        _, m, rows, calls = self.make_run()
        rows[0]["gold"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Gold values outside"):
            analyze(m, rows, calls)

    def test_unknown_names_are_private_in_confusion_and_distributions(self):
        response = {"text": "", "tool_calls": [{"name": "PRIVATE_TOOL_NAME", "arguments": "{}"}],
                    "served_models": ["correct"], "error": None}
        with patch.object(providers, "call", return_value=response):
            directory, _, _, _ = self.make_run(gold=False, draws=1)
        output = self.root / "unknown.html"
        build_report(directory, output)
        data = json.loads(output.with_suffix(".analysis.json").read_text())
        self.assertIsNone(data["tool_confusion"][0]["old_mode"])
        self.assertIsNone(data["unlabelled"]["items"][0]["old_mode"])
        self.assertEqual(data["hazard_summary"]["old"]["unknown_tool"], 3)
        for suffix in (".html", ".analysis.json", ".decision.json", ".decision.md"):
            self.assertNotIn("PRIVATE_TOOL_NAME", output.with_suffix(suffix).read_text())

    def test_definition_formats_and_invalid_definitions(self):
        canonical = read_tools(ROOT / "examples/tools.json")
        path = self.root / "tools.json"
        path.write_text(json.dumps([t["function"] for t in canonical]), encoding="utf-8-sig")
        self.assertEqual(read_tools(path), canonical)
        for definitions in ({}, [], [None], [{"type": "custom"}],
                            [{"name": "none", "description": "", "parameters": {}}],
                            [canonical[0], canonical[0]], [{"name": "a", "description": "", "parameters": []}],
                            [{"name": "a", "description": "", "parameters": {"type": "bogus"}}]):
            path.write_text(json.dumps(definitions))
            with self.assertRaises(ValueError):
                read_tools(path)

    def test_identical_mock_arms_have_no_bh_changes_across_seeds(self):
        tools = read_tools(ROOT / "examples/tools.json")
        for seed in range(40):
            rng = random.Random(seed)
            rows = [{"id": str(rng.getrandbits(64)), "input": text} for text in
                    ("order status", "refund request", "hello") * 2]
            manifest = {"answer": "tool", "models": {"old": "mock:flaky", "new": "mock:flaky"},
                        "draws": 12, "batch": 1, "labels": [t["function"]["name"] for t in tools] + ["none"],
                        "created_at": "fixture", "complete": True}
            calls = []
            for arm in ("old", "new"):
                for draw in range(12):
                    for i, row in enumerate(rows):
                        context = {"answer": "tool", "tools": tools, "seed": seed, "arm": arm,
                                   "item_id": row["id"], "input": row["input"], "draw": draw}
                        response = mock_tool("flaky", context)
                        self.assertEqual(response, mock_tool("flaky", context))
                        parsed = parse_tool_reply(response, tools)
                        calls.append({"arm": arm, "draw": draw, "batch": i, "attempts": [],
                                      "predictions": [{"answer": parsed["answer"]}], **parsed})
            data = analyze(manifest, rows, calls)
            self.assertFalse(any(item["changed"] for item in data["unlabelled"]["items"]), seed)


class ToolProviderTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp

    def test_openai_compatible_and_anthropic_recorded_stub_parsing(self):
        tools = read_tools(ROOT / "examples/tools.json")
        messages = [{"role": "user", "content": "Fictional support chat"},
                    {"role": "assistant", "content": "What is the code?"},
                    {"role": "user", "content": "DEMO"}]
        for provider in ("openai", "openai-compatible", "anthropic"):
            fixture = "anthropic" if provider == "anthropic" else "openai"
            stub = json.loads((ROOT / "tests/fixtures" / (fixture + "_tools.json")).read_text())
            with patch.dict(os.environ, {"OPENAI_API_KEY": "stub", "ANTHROPIC_API_KEY": "stub",
                                         "MIGRATION_CHECK_BASE_URL": "http://localhost/v1"}), \
                    patch.object(providers, "post", return_value=stub) as post:
                result = providers.call(provider + ":stub", "Support", messages, {"answer": "tool", "tools": tools})
            parsed = parse_tool_reply(result, tools)
            self.assertEqual(parsed["answer"], "order_status")
            self.assertEqual(parsed["n_tool_calls"], 2)
            self.assertEqual([c["args_valid"] for c in parsed["tool_calls"]], [True, False])
            self.assertIn("PRIVATE_ARGUMENT", parsed["tool_calls"][0]["arguments"])
            self.assertEqual(result["served_models"], ["stub-" + fixture])
            payload = post.call_args.args[1]
            if provider == "anthropic":
                self.assertEqual(result["stop_reason"], "tool_use")
                self.assertEqual(payload["messages"], messages)
                self.assertEqual(payload["tools"][0], {"name": "order_status", "description": tools[0]["function"]["description"],
                                                       "input_schema": tools[0]["function"]["parameters"]})
            else:
                self.assertEqual(result["finish_reason"], "tool_calls")
                self.assertIsNone(result["refusal"])
                self.assertEqual(payload["messages"][1:], messages)
                self.assertEqual(payload["tools"], tools)
                self.assertEqual(payload["tool_choice"], "auto")

    def test_cli_rejects_tools_without_invoking_subprocess(self):
        with patch.object(providers.subprocess, "run") as call:
            with self.assertRaisesRegex(ValueError, "does not support tool mode"):
                providers.call("claude-cli:m", "Support", [], {"answer": "tool", "tools": []})
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
