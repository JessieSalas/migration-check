"""Offline regression coverage for the independent fix-round findings."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
import io
import itertools
import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import urllib.error

import test_migration_check as base
from migration_check import providers, stats
from migration_check.__main__ import main, parser
from migration_check.analysis import analyze, load_run
from migration_check.protocol import parse_keyed, read_inputs, read_labels, validate_gold
from migration_check.report import build_report, markdown, private_view
from migration_check.runner import run


class FixTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp
    make_run = base.OfflineTests.make_run

    def test_salt_is_per_run_private_stable_and_not_exported(self):
        first, _, _, _ = self.make_run(gold=False)
        second, _, _, _ = self.make_run(gold=False, name="second")
        salt = (first / "privacy-salt").read_bytes()
        self.assertEqual(len(salt), 32)
        self.assertEqual((first / "privacy-salt").stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(salt, (second / "privacy-salt").read_bytes())
        output = self.root / "one.html"
        build_report(first, output)
        view = json.loads(output.with_suffix(".analysis.json").read_text())
        build_report(first, output)
        self.assertEqual(view, json.loads(output.with_suffix(".analysis.json").read_text()))
        build_report(second, self.root / "two.html")
        other = json.loads((self.root / "two.analysis.json").read_text())
        a = view["unlabelled"]["items"][0]["old_mode"]
        self.assertNotEqual(a, other["unlabelled"]["items"][0]["old_mode"])
        self.assertEqual(a, view["review_queue"][0]["old_counts"][0]["answer"])
        for suffix in (".html", ".analysis.json", ".decision.json", ".decision.md"):
            text = output.with_suffix(suffix).read_text()
            self.assertNotIn(salt.hex(), text)
            self.assertNotIn("privacy-salt", text)
        # Legacy captures receive a salt without changing subsequent pseudonyms.
        (second / "privacy-salt").unlink()
        build_report(second, self.root / "legacy.html")
        self.assertEqual(len((second / "privacy-salt").read_bytes()), 32)

    def test_low_power_bounds_and_recommendation(self):
        for r in (1, 2, 3, 4, 5):
            _, m, rows, calls = self.make_run(gold=False, draws=r, batch=2, name=str(r))
            data = analyze(m, rows, calls)
            power = data["unlabelled"]["power"]
            self.assertEqual(power["minimum_per_item_p"], 2 / math.comb(2 * r, r))
            self.assertEqual(power["no_item_can_reach_significance"], r < 4)
            self.assertGreaterEqual(power["recommended_draws"], 5)
            if r < 4:
                self.assertTrue(any("No item can reach BH significance" in w for w in data["warnings"]))
                self.assertFalse(data["review_queue"])
        m["batch"] = 1000
        many = [{"id": str(i), "input": "test"} for i in range(501)]
        data = analyze(m, many, [])
        self.assertTrue(any("no isolated item can reach BH significance regardless of R" in w for w in data["warnings"]))

    def test_model_ids_legacy_and_http_odd_types(self):
        _, m, rows, calls = self.make_run(draws=1)
        for value in (5, {"a": 1}, ["x"], None, True):
            calls[0]["attempts"][0]["served_models"] = [value]
            data = analyze(m, rows, calls)
            self.assertTrue(all(isinstance(v, str) for v in data["served_models"]["old"]))
            response = {"model": value, "choices": [{"message": {"content": "yes"}}]}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.object(providers, "post", return_value=response):
                result = providers.call("openai:m", "sys", [], {})
            self.assertEqual(result["served_models"], [])
        self.assertEqual(providers.model_ids(["a" * 1000]), ["a" * 200])

    def test_safe_provider_failure_stops_before_full_run_and_is_reported(self):
        with patch.object(providers, "call", side_effect=providers.ProviderError("provider HTTP 401: Invalid API key")) as call:
            directory, m, rows, calls = self.make_run(draws=100, batch=2, stopped=True)
        self.assertEqual(call.call_count, 6)
        self.assertEqual(len(calls), 2)
        self.assertFalse(m["complete"])
        self.assertTrue(all("401" in a["error"] for c in calls for a in c["attempts"]))
        output = self.root / "errors.html"
        build_report(directory, output)
        self.assertIn("provider HTTP 401", output.read_text())
        self.assertIn("every collected call failed at the provider", output.read_text())

    def test_gold_validation_names_all_bad_ids_and_case_hint(self):
        rows = [{"id": "a", "gold": "Yes"}, {"id": "b", "gold": "maybe"}]
        with self.assertRaisesRegex(ValueError, "'a'.*case mismatch.*'b'.*exact spelling and case"):
            validate_gold(rows, ["yes", "no"])
        with self.assertRaisesRegex(ValueError, "Gold values outside"):
            self.make_run(labels=["Yes", "no"])
        directory, m, inputs, calls = self.make_run(labels=["yes", "no"], name="valid")
        inputs[0]["gold"] = "Yes"
        (directory / "inputs.jsonl").write_text("\n".join(json.dumps(x) for x in inputs))
        with self.assertRaisesRegex(ValueError, "case mismatch"):
            load_run(directory)
        with self.assertRaisesRegex(ValueError, "case mismatch"):
            analyze(m, inputs, calls)

    def test_bom_inputs_labels_system_template_and_run_files(self):
        inputs, labels, system, template = [self.root / name for name in ("in", "labels", "sys", "template")]
        for path, text in ((inputs, '{"id":"a","input":"hello","gold":"yes"}\r\n'),
                           (labels, "yes\r\nno\r\n"), (system, "CUSTOM"), (template, "Text {input}")):
            path.write_text(text, encoding="utf-8-sig")
        self.assertEqual(read_labels(labels), ["yes", "no"])
        self.assertEqual(read_inputs(inputs)[0]["id"], "a")
        args = parser().parse_args(["run", "--inputs", str(inputs), "--labels", str(labels),
                                    "--system", str(system), "--template", str(template),
                                    "--old", "mock:old", "--new", "mock:old", "--draws", "2",
                                    "--out", str(self.root / "bom")])
        run(args)
        directory = self.root / "bom"
        for name in ("manifest.json", "calls.jsonl"):
            path = directory / name
            path.write_text(path.read_text(), encoding="utf-8-sig")
        m, rows, calls = load_run(directory)
        self.assertEqual(m["system"], "CUSTOM")
        self.assertEqual(m["template"], "Text {input}")
        self.assertEqual(analyze(m, rows, calls)["draw_strip"]["old"][0]["value"], 1)
        inputs.write_text('{"id":}')
        with self.assertRaisesRegex(ValueError, "input line 1:.*column"):
            read_inputs(inputs)

    def test_torn_final_record_warns_and_middle_record_names_line(self):
        directory, _, _, calls = self.make_run(draws=2)
        path = directory / "calls.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1] + [lines[-1][:-40]]))
        m, rows, remaining = load_run(directory)
        self.assertEqual(len(remaining), len(calls) - 1)
        self.assertTrue(any("torn calls.jsonl line" in w for w in analyze(m, rows, remaining)["warnings"]))
        path.write_text("\n".join([lines[0], '{"torn":', lines[2]]))
        with self.assertRaisesRegex(ValueError, "calls.jsonl line 2"):
            load_run(directory)

    def test_exports_unique_and_cross_audit_overwrite_refused(self):
        first, _, _, _ = self.make_run()
        second, _, _, _ = self.make_run(name="second")
        output = self.root / "report.html"
        build_report(first, output)
        path = output.with_suffix(".decision.json")
        decision = json.loads(path.read_text())
        decision["reviewer"] = "Ana"
        decision["notes"] = "keep this"
        path.write_text(json.dumps(decision))
        snapshot = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "different audit.*preserve reviewer"):
            build_report(second, output)
        self.assertEqual(path.read_text(), json.dumps(decision))
        self.assertEqual(output.read_bytes(), snapshot)
        build_report(second, self.root / "second.html")
        a = json.loads(output.with_suffix(".analysis.json").read_text())
        b = json.loads((self.root / "second.analysis.json").read_text())
        self.assertNotEqual(a["export_stem"], b["export_stem"])
        self.assertIn("data.export_stem+'.json'", output.read_text())
        decision["items"][0]["old_counts"][0]["answer"] = "<img src=x>&"
        self.assertIn("&lt;img src=x&gt;&amp;", markdown(decision))
        self.assertNotIn("<img", markdown(decision))
        self.assertIn("replaceAll('<','&lt;')", output.read_text())

    def test_bounded_keyed_extraction(self):
        self.assertEqual(parse_keyed("{" * 1_000_000, 2), [None, None])
        self.assertEqual(parse_keyed("{" * 2000 + "}" * 2000, 2), [None, None])

    def test_ctrl_c_keeps_all_finished_provider_calls(self):
        inputs = self.root / "inputs.jsonl"
        inputs.write_text('{"id":"a","input":"hi"}\n')
        output, paid = self.root / "interrupted", self.root / "paid.jsonl"
        script = '''
import json, sys, time, threading
from migration_check import providers
from migration_check.__main__ import main
original = providers.mock
lock = threading.Lock()
def slow(model, context):
    time.sleep(.05)
    result = original(model, context)
    with lock:
        with open(sys.argv[3], 'a') as stream:
            stream.write(json.dumps([model, context['draw'], context['batch']]) + '\\n')
    return result
providers.mock = slow
raise SystemExit(main(['run','--inputs',sys.argv[1],'--out',sys.argv[2],
                      '--old','mock:old','--new','mock:new','--draws','50','--workers','4']))
'''
        proc = subprocess.Popen([sys.executable, "-c", script, str(inputs), str(output), str(paid)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not paid.exists() or len(paid.read_text().splitlines()) < 6:
                if proc.poll() is not None or time.monotonic() > deadline:
                    self.fail("Mock child did not reach the interrupt checkpoint")
                time.sleep(.01)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 130, (stdout, stderr))
        self.assertIn("interrupted; partial run kept", stderr)
        self.assertNotIn("Traceback", stderr)
        m, rows, calls = load_run(output)
        completed = [json.loads(line) for line in paid.read_text().splitlines()]
        self.assertEqual(len(calls), len(completed))
        self.assertLess(len(calls), 100)
        self.assertEqual({(c["arm"], c["draw"], c["batch"]) for c in calls}, {tuple(c) for c in completed})
        self.assertFalse(m["complete"])


class ProviderFixTests(unittest.TestCase):
    setUp = base.ProviderTests.setUp

    def test_compatible_does_not_reuse_openai_key(self):
        response = {"choices": [{"message": {"content": "yes"}}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-only", "MIGRATION_CHECK_BASE_URL": "http://localhost/v1"}, clear=True), patch.object(providers, "post", return_value=response) as post:
            providers.call("openai-compatible:m", "sys", [], {})
        self.assertEqual(post.call_args.args[2], {})

    def test_compatible_key_requires_https_except_loopback(self):
        for host in ("localhost", "127.0.0.1", "[::1]", "127.1.2.3"):
            with patch.dict(os.environ, {"MIGRATION_CHECK_BASE_URL": "http://" + host + "/v1", "MIGRATION_CHECK_API_KEY": "test"}):
                providers.preflight("openai-compatible:m")
        for host in ("192.168.1.5", "example.invalid", "localhost.example.invalid", "[::]"):
            with patch.dict(os.environ, {"MIGRATION_CHECK_BASE_URL": "http://" + host + "/v1", "MIGRATION_CHECK_API_KEY": "test"}):
                with self.assertRaisesRegex(ValueError, "Refusing.*HTTP.*non-loopback.*HTTPS"):
                    providers.preflight("openai-compatible:m")
                with self.assertRaisesRegex(ValueError, "Refusing"):
                    providers.call("openai-compatible:m", "sys", [], {})

    def test_http_safe_error_and_response_size(self):
        body = {"error": {"message": "Invalid API key unit-secret; PRIVATE_PROMPT"}}
        error = urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(json.dumps(body).encode()))
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = error
        with patch.dict(os.environ, {"MIGRATION_CHECK_API_KEY": "unit-secret"}), patch.object(providers.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.post("https://example.invalid", {"messages": [{"content": "PRIVATE_PROMPT"}]}, {}, 1)
        self.assertIn("HTTP 401: Invalid API key", str(caught.exception))
        self.assertNotIn("unit-secret", str(caught.exception))
        self.assertNotIn("PRIVATE_PROMPT", str(caught.exception))
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError("PRIVATE_URL"))
        with patch.object(providers.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(providers.ProviderError, "unreachable: connection refused"):
                providers.post("https://example.invalid", {}, {}, 1)
        opener.open.side_effect = None
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = b"x" * (providers.MAX_RESPONSE_BYTES + 1)
        with patch.object(providers.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(providers.ProviderError, "4 MiB"):
                providers.post("https://example.invalid", {}, {}, 1)
        response.read.assert_called_once_with(providers.MAX_RESPONSE_BYTES + 1)


class DistributionTests(unittest.TestCase):
    def test_tv_shuffle_fallback_matches_exact_null(self):
        old, new = ["a", "a", "b", "c"], ["a", "c", "c", "c"]
        _, _, exact = stats.distribution_test(old, new)
        with patch.object(stats, "tv_null", return_value=None):
            _, p, unavailable = stats.distribution_test(old, new, permutations=5000, seed=73)
        self.assertIsNone(unavailable)
        self.assertLess(abs(float(p - exact)), .03)

    def test_tv_matches_exhaustive_labelled_reallocations(self):
        for old, new in ((["a", "a"], ["b", "c"]), (["a", "b", "c"], ["b", "b", "c"]),
                         ([None, "a", "b", "b"], ["c", "c", "a", None])):
            r, pooled = len(old), old + new
            observed, p, exact = stats.distribution_test(old, new)
            null = []
            for chosen in itertools.combinations(range(2 * r), r):
                selected = set(chosen)
                a = Counter(pooled[i] for i in selected)
                b = Counter(pooled[i] for i in range(2 * r) if i not in selected)
                null.append(sum(abs(a[v] - b[v]) for v in set(pooled)))
            self.assertEqual(exact, Fraction(sum(v >= observed for v in null), len(null)))
            self.assertLess(abs(float(p - exact)), .02)
            self.assertEqual((observed, p, exact), stats.distribution_test(list(reversed(old)), list(reversed(new))))
            self.assertEqual((observed, p, exact), stats.distribution_test(new, old))

    def test_analysis_tied_modes_and_verdict_do_not_depend_on_order(self):
        manifest = {"models": {"old": "mock:old", "new": "mock:new"}, "draws": 20,
                    "batch": 1, "labels": [], "complete": True, "created_at": "test"}
        rows = [{"id": "x", "input": "test"}]
        def calls(reverse):
            result = []
            for arm, values in (("old", ["a"] * 10 + ["b"] * 10), ("new", ["c"] * 20)):
                for d, value in enumerate(values[::-1] if reverse else values):
                    result.append({"arm": arm, "draw": d, "batch": 0, "predictions": [{"answer": value}], "attempts": []})
            return result
        a, b = [analyze(manifest, rows, calls(reverse)) for reverse in (False, True)]
        self.assertEqual(a["unlabelled"], b["unlabelled"])
        self.assertTrue(a["unlabelled"]["items"][0]["changed"])

    def test_identical_mock_arm_distributions_bh_calibration(self):
        # Independent draws from identical uniform arms. Under the complete
        # null, the fraction of audits with any BH rejection estimates FDR.
        # Exercise the larger-R, many-valued regime that exposed selection bias.
        audits, items, r = 200, 10, 50
        discoveries = 0
        for seed in range(audits):
            rng = random.Random(seed)
            pvalues = [stats.distribution_test(rng.choices(range(10), k=r), rng.choices(range(10), k=r))[1]
                       for _ in range(items)]
            discoveries += any(p <= stats.ALPHA for p in stats.bh(pvalues))
        self.assertLessEqual(discoveries / audits, .05, f"BH null FDR: {discoveries}/{audits}")


if __name__ == "__main__":
    unittest.main()
