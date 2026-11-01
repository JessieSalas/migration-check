"""Run with python -m unittest discover -s tests -v. All network access is denied."""

from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
import io
import itertools
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from migration_check import providers, stats
from migration_check.__main__ import main, parser
from migration_check.analysis import analyze, load_run
from migration_check.protocol import SYSTEM, build_request, parse_keyed, pattern, read_inputs
from migration_check.report import build_report, private_view
from migration_check.runner import run

ROOT = Path(__file__).resolve().parents[1]


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        blocker = patch.object(socket.socket, "connect", side_effect=AssertionError("Tests must not access network"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def make_run(self, gold=True, old="mock:old", new="mock:new", batch=1, draws=10, labels=None, name="run", stopped=False):
        rows = [{"id": "a", "input": "LOCAL_INPUT_PRIVATE", "slice": "one"},
                {"id": "b", "messages": [{"role": "user", "content": "hello"}], "slice": "two"}]
        if gold:
            rows[0]["gold"], rows[1]["gold"] = "yes", "no"
        path = self.root / (name + ".jsonl")
        path.write_text("\n".join(json.dumps(row) for row in rows))
        argv = ["run", "--inputs", str(path), "--old", old, "--new", new, "--batch", str(batch),
                "--draws", str(draws), "--workers", "3", "--out", str(self.root / name)]
        if labels is not None:
            lp = self.root / (name + ".labels")
            lp.write_text("\n".join(labels))
            argv += ["--labels", str(lp)]
        if stopped:
            with self.assertRaisesRegex(ValueError, "Stopped early"):
                run(parser().parse_args(argv))
            result = {"directory": str(self.root / name)}
        else:
            result = run(parser().parse_args(argv))
        manifest, inputs, calls = load_run(result["directory"])
        return Path(result["directory"]), manifest, inputs, calls

    def test_missing_claude_cli_stops_run_before_any_file(self):
        inputs = self.root / "inputs.jsonl"
        inputs.write_text(json.dumps({"id": "a", "input": "synthetic text"}) + "\n")
        out = self.root / "never"
        argv = ["run", "--inputs", str(inputs), "--old", "claude-cli:old-model", "--new", "claude-cli:new-model",
                "--out", str(out)]
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "(?s)claude CLI not found on PATH.*npm install"):
                run(parser().parse_args(argv))
        self.assertFalse(out.exists())

    def test_failed_start_can_be_rerun_into_the_same_directory(self):
        inputs = self.root / "inputs.jsonl"
        inputs.write_text("".join(json.dumps({"id": i, "input": "synthetic " + i}) + "\n" for i in "abc"))
        out = self.root / "audit"
        argv = lambda old: ["run", "--inputs", str(inputs), "--old", old, "--new", "mock:fail", "--draws", "2",
                            "--out", str(out)]
        with self.assertRaisesRegex(ValueError, "rerunning the same command reuses that directory"):
            run(parser().parse_args(argv("mock:fail")))
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaisesRegex(ValueError, "Stopped early"):
            run(parser().parse_args(argv("mock:fail")))
        self.assertIn("moved to", err.getvalue())
        aside = [p for p in out.iterdir() if p.name.startswith("failed-start-")]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "calls.jsonl").is_file())
        # Once one arm has answered, the directory holds evidence and is never reused.
        other = self.root / "partial"
        argv2 = ["run", "--inputs", str(inputs), "--old", "mock:old", "--new", "mock:fail", "--draws", "2",
                 "--out", str(other)]
        with self.assertRaisesRegex(ValueError, "remove it \\(rm -r"):
            run(parser().parse_args(argv2))
        with self.assertRaisesRegex(ValueError, "already contains audit data"):
            run(parser().parse_args(argv2))

    def test_gold_mock_end_to_end(self):
        directory, manifest, rows, calls = self.make_run(labels=["yes", "no"], batch=2)
        self.assertEqual(len(calls), 20)
        data = analyze(manifest, rows, calls)
        self.assertEqual(data["gold"]["Dstar"], 0)
        self.assertEqual([q["kind"] for q in data["review_queue"]], ["stable_regression", "stable_improvement"])
        self.assertEqual([q["expected_drop"] for q in data["review_queue"]], [1, -1])
        self.assertEqual(len(data["gold"]["primary_min_p"]["slice_tests"]), 2)
        output = self.root / "report.html"
        build_report(directory, output)
        self.assertTrue(output.exists())
        self.assertTrue(output.with_suffix(".decision.md").exists())
        decision = json.loads(output.with_suffix(".decision.json").read_text())
        self.assertTrue(all(q["reviewer_status"] == "pending" for q in decision["items"]))
        decision["items"][0]["reviewer_status"] = "follow_up"
        output.with_suffix(".decision.json").write_text(json.dumps(decision))
        build_report(directory, output)
        updated = json.loads(output.with_suffix(".decision.json").read_text())
        self.assertEqual(updated["items"][0]["reviewer_status"], "follow_up")

    def test_without_gold_end_to_end_and_distributions(self):
        directory, manifest, rows, calls = self.make_run(gold=False, batch=2, labels=["yes", "no"])
        data = analyze(manifest, rows, calls)
        self.assertIsNone(data["gold"])
        self.assertEqual(len(data["review_queue"]), 2)
        for item in data["unlabelled"]["items"]:
            self.assertTrue(item["changed"])
            self.assertEqual(item["old_counts"], [{"answer": "yes", "count": 10}])
            self.assertEqual(item["new_counts"], [{"answer": "no", "count": 10}])
            self.assertLessEqual(item["adjusted_p"], .05)
        self.assertEqual(data["self_disagreement_floor"]["old"]["mean_flip_rate"], 0)
        output = self.root / "no-gold.html"
        build_report(directory, output)
        self.assertIn("CHANGED", output.read_text())

    def test_keyed_parse_missing_never_shifts(self):
        self.assertEqual(parse_keyed('prefix {"3":"third", "1":"first"} suffix', 3), ["first", None, "third"])
        for raw in ("bad", "[]", "{}", '{"1":null}', '{"1":{}}', '{"1":42}', '{"1":""}'):
            self.assertEqual(parse_keyed(raw, 2), [None, None])
        self.assertEqual(parse_keyed('{"2":"ok"}', 2), [None, "ok"])

    def test_batch_one_is_single_reply_and_tail_batch_is_keyed(self):
        directory, manifest, rows, calls = self.make_run(batch=1, draws=1)
        self.assertTrue(all(call["attempts"][0]["raw_text"] in ("yes", "no") for call in calls))
        # The last short batch must retain JSON format when configured batch >1.
        directory, manifest, rows, calls = self.make_run(batch=3, draws=1, name="short")
        self.assertEqual(json.loads(calls[0]["attempts"][0]["raw_text"]), {"1": "yes", "2": "yes"})
        self.assertEqual(len(calls), 2)

    def test_failures_retries_and_partial_missing_are_counted(self):
        _, manifest, rows, calls = self.make_run(new="mock:fail", batch=2, draws=2, stopped=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse(manifest["complete"])
        new_calls = [c for c in calls if c["arm"] == "new"]
        self.assertTrue(all(len(c["attempts"]) == 3 for c in new_calls))
        self.assertTrue(all(a["raw_text"] == "MOCK_RAW_FAILURE_PRIVATE" for c in new_calls for a in c["attempts"]))
        data = analyze(manifest, rows, calls)
        summary = data["hazard_summary"]["new"]
        self.assertEqual(summary["parse_failures"], 4)
        self.assertEqual(summary["attempt_missing_answers"], 6)
        self.assertEqual(summary["failed_attempts"], 3)
        self.assertEqual(summary["retries"], 2)
        self.assertEqual([d["value"] for d in data["draw_strip"]["new"]], [0, 0])
        _, m, inputs, partial = self.make_run(new="mock:partial", batch=2, draws=1, name="partial")
        data = analyze(m, inputs, partial)
        self.assertEqual(data["hazard_summary"]["new"]["parse_failures"], 1)
        self.assertEqual(data["hazard_summary"]["new"]["failed_attempts"], 1)
        self.assertEqual(data["hazard_summary"]["new"]["retries"], 0)

    def test_recovered_failures_remain_visible(self):
        _, m, rows, calls = self.make_run(new="mock:retry", batch=2, draws=2)
        data = analyze(m, rows, calls)
        self.assertEqual(data["hazard_summary"]["new"]["parse_failures"], 0)
        self.assertEqual(data["hazard_summary"]["new"]["failed_attempts"], 2)
        self.assertEqual(data["hazard_summary"]["new"]["retries"], 2)
        self.assertEqual(len([a for c in calls if c["arm"] == "new" for a in c["attempts"]]), 4)

    def test_provider_exceptions_are_durable_and_do_not_leak_messages(self):
        with patch.object(providers, "call", side_effect=TimeoutError("PRIVATE_EXCEPTION_DETAIL")):
            _, m, rows, calls = self.make_run(batch=2, draws=1, stopped=True)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(c["attempts"]) == 3 for c in calls))
        self.assertTrue(all(a["error"] == "TimeoutError" for c in calls for a in c["attempts"]))
        self.assertNotIn("PRIVATE_EXCEPTION_DETAIL", json.dumps(calls))
        data = analyze(m, rows, calls)
        self.assertEqual(data["hazard_summary"]["old"]["parse_failures"], 2)
        self.assertEqual(data["hazard_summary"]["new"]["parse_failures"], 2)

    def test_free_text_normalization_preserves_classification_hazards(self):
        _, m, rows, calls = self.make_run(gold=False, draws=2)
        for c in calls:
            c["predictions"][0]["answer"] = " Yes \n please " if c["arm"] == "old" else "yes please"
        data = analyze(m, rows, calls)
        self.assertTrue(all(not item["changed"] for item in data["unlabelled"]["items"]))
        self.assertEqual(data["unlabelled"]["items"][0]["old_mode"], "yes please")
        m["labels"] = ["yes please"]
        data = analyze(m, rows, calls)
        self.assertGreater(data["hazard_summary"]["old"]["off_list"], 0)

    def test_off_list_patterns_and_strict_spelling(self):
        self.assertEqual(pattern("general_praise", ["general praise"]), "snake_case")
        self.assertEqual(pattern("YES", ["yes"]), "case_change")
        self.assertEqual(pattern("The answer is yes.", ["yes"]), "extra_text")
        _, m, rows, calls = self.make_run(gold=False, new="mock:snake", labels=["first label", "general praise"], batch=2)
        data = analyze(m, rows, calls)
        self.assertEqual(data["hazard_summary"]["new"]["off_list"], 20)
        self.assertEqual(data["hazard_summary"]["new"]["dominant_pattern"], "snake_case")
        self.assertTrue(all(row["value"] == 0 for row in data["draw_strip"]["new"]))

    def test_served_mismatch_and_missing_id_warning(self):
        _, m, rows, calls = self.make_run(new="mock:mismatch", draws=2)
        data = analyze(m, rows, calls)
        self.assertTrue(any("Served-model mismatch" in w for w in data["warnings"]))
        self.assertEqual(data["served_models"]["new"], ["unexpected-model"])
        calls[0]["attempts"][0]["served_models"] = []
        self.assertTrue(any("no served-model ID" in w for w in analyze(m, rows, calls)["warnings"]))

    def test_incomplete_call_counts_and_duplicate_rejection(self):
        directory, m, rows, calls = self.make_run(draws=2, batch=2)
        # Calls are written by parallel workers, so the file order of arms varies: drop the last NEW call
        # explicitly (dropping calls[-1] made this test fail whenever an old-arm call happened to be last).
        last_new = max(i for i, c in enumerate(calls) if c["arm"] == "new")
        data = analyze(m, rows, calls[:last_new] + calls[last_new + 1:])
        self.assertEqual(data["hazard_summary"]["new"]["parse_failures"], 2)
        self.assertTrue(any("Incomplete run" in w for w in data["warnings"]))
        with (directory / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps(calls[0]) + "\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_run(directory)

    def test_report_privacy_and_safe_embedding(self):
        directory, m, rows, calls = self.make_run(gold=False, new="mock:fail", batch=2, stopped=True)
        # Inject valid but sensitive replies and a script-closing input ID.
        marker = 'PRIVATE_REPLY_</script><script>alert("x")</script>'
        for c in calls:
            if c["arm"] == "old":
                c["attempts"][0]["raw_text"] = marker
                for p in c["predictions"]:
                    p["answer"] = marker
        (directory / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
        output = self.root / "private.html"
        build_report(directory, output)
        hidden = output.read_text()
        self.assertNotIn("MOCK_RAW_FAILURE_PRIVATE", hidden)
        self.assertNotIn("PRIVATE_REPLY", hidden)
        self.assertNotIn("LOCAL_INPUT_PRIVATE", hidden)
        self.assertNotIn("innerHTML", hidden)
        self.assertNotIn('<script src=', hidden)
        for suffix in (".decision.json", ".decision.md", ".analysis.json"):
            self.assertNotIn("PRIVATE_REPLY", output.with_suffix(suffix).read_text())
        build_report(directory, self.root / "visible.html", show_text=True)
        visible = (self.root / "visible.html").read_text()
        self.assertIn("MOCK_RAW_FAILURE_PRIVATE", visible)
        self.assertIn("PRIVATE_REPLY", visible)
        self.assertIn("LOCAL_INPUT_PRIVATE", visible)
        self.assertNotIn(marker, visible)
        self.assertIn("\\u003c/script\\u003e", visible)

    def test_hashed_distribution_consistency(self):
        _, m, rows, calls = self.make_run(gold=False)
        view = private_view(analyze(m, rows, calls))
        self.assertEqual(view["unlabelled"]["items"][0]["old_mode"], view["review_queue"][0]["old_counts"][0]["answer"])

    def test_self_disagreement_and_mixed_gold(self):
        _, m, rows, calls = self.make_run(new="mock:flaky", draws=10)
        rows[1].pop("gold")
        data = analyze(m, rows, calls)
        self.assertEqual(data["gold"]["n_items"], 1)
        self.assertEqual(data["unlabelled"]["n_items"], 1)
        self.assertEqual(data["self_disagreement_floor"]["new"]["mean_flip_rate"], .5)
        self.assertAlmostEqual(data["self_disagreement_floor"]["new"]["pairwise_disagreement"], 5 / 9)

    def test_inputs_validation_and_no_gold_in_prompts(self):
        row = {"id": "a", "input": "hello", "gold": "SECRET_GOLD", "slice": "SECRET_SLICE"}
        system, messages = build_request([row], ["yes"], template="Literal {x}: {input}", batched=True)
        payload = json.dumps(messages)
        self.assertNotIn("SECRET_GOLD", payload)
        self.assertNotIn("SECRET_SLICE", payload)
        self.assertIn("Literal {x}: hello", payload)
        self.assertIn('\\"1\\"', payload)
        path = self.root / "bad.jsonl"
        for bad in ([row, row], [{"id": "a", "input": "hello", "messages": []}], [{"id": "a", "input": 1}],
                    [{"id": "a", "messages": [{"role": "tool", "content": "x"}]}]):
            path.write_text("\n".join(json.dumps(x) for x in bad))
            with self.assertRaises(ValueError):
                read_inputs(path)

    def test_single_chat_roles_and_custom_system(self):
        row = {"messages": [{"role": "system", "content": "custom"}, {"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"}, {"role": "user", "content": "classify"}]}
        system, messages = build_request([row], [], system="base")
        self.assertEqual(system, "base\n\ncustom")
        self.assertEqual(messages, row["messages"][1:])

    def test_cli_help_and_invalid_parameters(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--inputs", "unused", "--old", "mock:old", "--new", "mock:new",
                                   "--draws", "0", "--out", str(self.root / "bad")]), 2)


class StatisticsTests(unittest.TestCase):
    def test_bh_exact_ties(self):
        values = [Fraction(1, 100), Fraction(4, 100), Fraction(4, 100), Fraction(1)]
        self.assertEqual(stats.bh(values), [Fraction(1, 25), Fraction(4, 75), Fraction(4, 75), Fraction(1)])
        self.assertEqual(stats.bh([]), [])

    def test_permutation_against_exhaustive_synthetic(self):
        for r, ki, kc in [(2, 2, 0), (3, 3, 0), (4, 3, 1), (4, 2, 2)]:
            pooled = [1] * (ki + kc) + [0] * (2 * r - ki - kc)
            stats_null = [abs(2 * sum(pooled[i] for i in chosen) - ki - kc)
                          for chosen in itertools.combinations(range(2 * r), r)]
            exact = Fraction(sum(v >= abs(ki - kc) for v in stats_null), len(stats_null))
            self.assertEqual(stats.exact_item_p(ki, kc, r), exact)
            mc = stats.permutation_p(ki, kc, r)
            self.assertLess(abs(float(mc - exact)), .02)
            self.assertEqual(mc, stats.permutation_p(ki, kc, r))
        self.assertEqual(stats.exact_item_p(10, 0, 10), Fraction(2, math.comb(20, 10)))
        self.assertLess(stats.permutation_p(10, 0, 10), Fraction(1, 20))

    def test_minp_inclusive_ties_and_exact_denominators(self):
        observed = [2, -1]
        null = [[2, 0], [-2, 1], [0, -1], [0, 0]]
        raw, adjusted = stats.minp_test(observed, null, exhaustive=True)
        self.assertEqual(raw, [Fraction(1, 2), Fraction(1, 2)])
        self.assertEqual(adjusted, [Fraction(3, 4), Fraction(3, 4)])
        raw, adjusted = stats.minp_test(observed, null)
        self.assertEqual(raw, [Fraction(3, 5), Fraction(3, 5)])
        self.assertEqual(adjusted, [Fraction(4, 5), Fraction(4, 5)])

    def test_graded_primary_matches_reference(self):
        if stats.np is None:
            self.skipTest("Reference comparison requires optional NumPy")
        # Vendored outputs from graded2 primary, NumPy seed 0, 10,000 draws.
        a = [[int(d < 7), int(d < 6), d % 2, int(d < 3)] for d in range(10)]
        b = [[int(d < 4), int(d < 4), int(d < 7), int(d < 6)] for d in range(10)]
        p = [Fraction(2174, 10001), Fraction(2049, 10001)]
        adjusted = [Fraction(3795, 10001), Fraction(2531, 10001)]
        result = stats.graded_test(a, b, ["s1", "s1", "s2", "s2"])
        self.assertEqual([row["p"] for row in result["slice_tests"]], [float(x) for x in p])
        self.assertEqual([row["adjusted_p"] for row in result["slice_tests"]], [float(x) for x in adjusted])

    def test_stdlib_graded_fallback_and_null(self):
        a = [[1, 1, 0] for _ in range(10)]
        b = [[0, 0, 1] for _ in range(10)]
        with patch.object(stats, "np", None):
            result = stats.graded_test(a, b, ["worse", "worse", "better"])
            same = stats.graded_test(a, a, ["worse", "worse", "better"])
        self.assertEqual(result["worse"][0]["slice"], "worse")
        self.assertEqual(result["better"][0]["slice"], "better")
        self.assertEqual(same["worse"], [])
        self.assertEqual(same["better"], [])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        blocker = patch.object(socket.socket, "connect", side_effect=AssertionError("No network"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.context = {"labels": [], "draw": 0, "batch": 0, "attempt": 0, "size": 1, "batched": False}

    def test_openai_and_compatible_payloads(self):
        response = {"model": "served", "choices": [{"message": {"content": "answer"}}], "usage": {"total_tokens": 12}}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-secret", "MIGRATION_CHECK_API_KEY": "compatible-secret", "MIGRATION_CHECK_BASE_URL": "http://localhost:123/v1"}), patch.object(providers, "post", return_value=response) as post:
            for provider, url in [("openai", "https://api.openai.com/v1/chat/completions"),
                                  ("openai-compatible", "http://localhost:123/v1/chat/completions")]:
                result = providers.call(provider + ":requested", "sys", [{"role": "user", "content": "hello"}], self.context)
                self.assertEqual(result["served_models"], ["served"])
                self.assertEqual(result["text"], "answer")
                self.assertEqual(post.call_args.args[0], url)
                self.assertEqual(post.call_args.args[1]["messages"][0], {"role": "system", "content": "sys"})
                self.assertEqual(post.call_args.args[2]["Authorization"], "Bearer " + ("unit-secret" if provider == "openai" else "compatible-secret"))

    def test_anthropic_payload_and_response(self):
        response = {"model": "served-claude", "content": [{"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "yes"}], "usage": {"input_tokens": 12}}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "unit-secret"}), patch.object(providers, "post", return_value=response) as post:
            result = providers.call("anthropic:requested", "sys", [{"role": "user", "content": "hi"}], self.context)
        self.assertEqual(result["text"], "yes")
        self.assertEqual(result["served_models"], ["served-claude"])
        self.assertEqual(post.call_args.args[1]["system"], "sys")
        self.assertEqual(post.call_args.args[2]["x-api-key"], "unit-secret")

    def test_claude_flags_isolation_and_model_usage(self):
        def fake_run(argv, **kwargs):
            self.assertEqual(argv[:-2], ["claude", "-p", "--model", "requested"] + providers.CLAUDE_FLAGS)
            self.assertIn("--no-session-persistence", argv)
            self.assertEqual(argv[-2], "--system-prompt-file")
            system_path = Path(argv[-1])
            self.assertEqual(system_path.read_text(), "PRIVATE_SYSTEM")
            self.assertEqual(system_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("PRIVATE_SYSTEM", argv)
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [system_path])
            self.assertNotEqual(Path(kwargs["cwd"]), ROOT)
            # Claude Code's own nonessential traffic is switched off for every call.
            self.assertEqual(kwargs["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "yes", "modelUsage": {"actual-model": {"inputTokens": 12}}}), "")
        with patch.object(providers.subprocess, "run", side_effect=fake_run):
            result = providers.call("claude-cli:requested", "PRIVATE_SYSTEM", [{"role": "user", "content": "hello"}], self.context)
        self.assertEqual(result["served_models"], ["actual-model"])
        self.assertEqual(result["text"], "yes")
        self.assertEqual(providers.CLAUDE_FLAGS,
                         ["--output-format", "json", "--tools", "", "--setting-sources", "",
                          "--strict-mcp-config", "--no-session-persistence"])

    def test_no_redirect_and_key_preflight(self):
        self.assertIsNone(providers.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                providers.preflight("openai:some-model")
            with self.assertRaisesRegex(ValueError, "MIGRATION_CHECK_BASE_URL"):
                providers.preflight("openai-compatible:some-model")
        for spec in ("unknown:model", "openai:", "no-colon"):
            with self.assertRaises(ValueError):
                providers.split_model(spec)


if __name__ == "__main__":
    unittest.main()
