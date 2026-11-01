"""Offline archive integrity, privacy and replay regression tests."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_migration_check as base
import test_tools
from migration_check import providers
from migration_check.__main__ import main, parser
from migration_check.analysis import analyze, load_run
from migration_check.archive import canonical, chain_hash, create_archive, digest, inputs_digest, load_archive, manifest_seed, verify_archive
from migration_check.privacy import answer_key
from migration_check.protocol import normalize
from migration_check.report import build_report, markdown, private_view
from migration_check.runner import run
from migration_check.tools import TOOL_HAZARDS


class ArchiveTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp
    make_run = base.OfflineTests.make_run

    def freeze(self, directory, arm="old", name="archive", include_raw=False):
        output = self.root / name
        create_archive(directory, arm, output, include_raw)
        return output

    def replay(self, source, archive, new="mock:new", name="replayed", extra=()):
        original = json.loads((source / "manifest.json").read_text())
        argv = ["run", "--inputs", str(source / "inputs.jsonl"), "--old", "archive:" + str(archive),
                "--new", new, "--batch", str(original["batch"]), "--draws", "99",
                "--out", str(self.root / name)]
        if original["answer"] == "tool":
            tools = self.root / "replay-tools.json"
            tools.write_text(json.dumps(original["tools"]))
            argv += ["--answer", "tool", "--tools", str(tools)]
        elif original["labels"]:
            labels = self.root / "replay-labels.txt"
            labels.write_text("\n".join(original["labels"]))
            argv += ["--labels", str(labels)]
        for key in ("system", "template"):
            if original.get(key) is not None:
                path = self.root / ("replay-" + key + ".txt")
                path.write_text(original[key])
                argv += ["--" + key, str(path)]
        with redirect_stdout(io.StringIO()):
            run(parser().parse_args(argv + list(extra)))
        return self.root / name, *load_run(self.root / name)

    def without_archive_metadata(self, analysis):
        result = dict(analysis)
        for key in ("archived_old", "prompt_changes", "requested_draws", "draws_notice"):
            result.pop(key, None)
        result["warnings"] = [warning for warning in result["warnings"]
                              if not warning.startswith(("Old arm ARCHIVED:", "Archive has R="))]
        return result

    def assert_same_statistics(self, live, replay):
        self.assertEqual(self.without_archive_metadata(live)["warnings"],
                         self.without_archive_metadata(replay)["warnings"])
        for key in ("gold", "hazards", "hazard_summary", "draw_strip", "self_disagreement_floor", "tool_confusion"):
            self.assertEqual(live[key], replay[key], key)
        for a, b in zip(live["unlabelled"]["items"], replay["unlabelled"]["items"]):
            for key in ("changed", "p", "adjusted_p", "exact_enumerated_p", "statistic_numerator"):
                self.assertEqual(a[key], b[key], key)

    def test_chain_detects_one_byte_edits_deletions_and_reordering(self):
        source, _, _, _ = self.make_run(draws=3, labels=["yes", "no"])
        archive = self.freeze(source)
        self.assertTrue(verify_archive(archive)["verified"])
        answers = archive / "answers.jsonl"
        original = answers.read_bytes()
        variants = [original.replace(b'"yes"', b'"Yes"', 1), original[:-1],
                    b"".join(reversed(original.splitlines(keepends=True))),
                    b"".join(original.splitlines(keepends=True)[:-1])]
        for changed in variants:
            answers.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "hash chain|Invalid archive"):
                verify_archive(archive)
        answers.write_bytes(original)
        self.assertTrue(verify_archive(archive)["verified"])
        manifest_path = archive / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes().replace(b'"batch":1', b'"batch":2'))
        with self.assertRaisesRegex(ValueError, "manifest checksum"):
            verify_archive(archive)

    def test_manifest_hashes_privacy_and_raw_opt_in(self):
        source, manifest, rows, calls = self.make_run(gold=False, draws=2, old="mock:extra")
        archive = self.freeze(source)
        frozen, entries, salt = load_archive(archive)
        self.assertEqual(frozen["inputs_sha256"], inputs_digest(rows))
        self.assertEqual(frozen["system_sha256"], hashlib.sha256(manifest["system"].encode()).hexdigest())
        self.assertEqual(frozen["template_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertIsNone(frozen["labels_sha256"])
        self.assertIsNone(frozen["tools_sha256"])
        self.assertEqual(len(entries), len(rows) * manifest["draws"])
        old_call = next(c for c in calls if c["arm"] == "old")
        expected = answer_key(normalize(old_call["predictions"][0]["answer"], []), [], salt)
        self.assertEqual(entries[0]["answer"], expected)
        for filename in ("answers.jsonl", "manifest.json"):
            text = (archive / filename).read_text()
            for secret in ("LOCAL_INPUT_PRIVATE", manifest["system"], old_call["attempts"][0]["raw_text"]):
                self.assertNotIn(secret, text)
            self.assertNotIn('"raw_text"', text)
        self.assertEqual((archive / "privacy-salt").stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(e["provider"] == "mock" and e["served_models"] == ["extra"] for e in entries))
        self.assertTrue(all(e["timestamp_source"] == "call" for e in entries))
        raw = self.freeze(source, name="raw", include_raw=True)
        self.assertEqual(load_archive(raw)[1][0]["raw_text"], [old_call["attempts"][0]["raw_text"]])

    def test_labelled_replay_matches_entire_analysis_and_calls_only_candidate(self):
        source, m, rows, calls = self.make_run(draws=5, labels=["yes", "no"], batch=3)
        archive = self.freeze(source)
        original_call = providers.call
        def candidate_only(spec, *args, **kwargs):
            self.assertEqual(spec, "mock:new")
            return original_call(spec, *args, **kwargs)
        with patch.object(providers, "call", side_effect=candidate_only) as mocked:
            replayed, rm, rr, rc = self.replay(source, archive)
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(rm["draws"], 5)
        live, replay = analyze(m, rows, calls), analyze(rm, rr, rc)
        replay.pop("answers_hashed")
        replay["created_at"] = live["created_at"]
        self.assertEqual(live, self.without_archive_metadata(replay))
        build_report(replayed, self.root / "replay.html")
        # Replay remains self-contained if the original archive moves away.
        shutil.rmtree(archive)
        build_report(replayed, self.root / "replay.html")

    def test_free_text_replay_normalizes_hashes_gold_and_ties(self):
        for gold in (False, True):
            source, m, rows, calls = self.make_run(gold=gold, draws=4, old="mock:flaky", name=str(gold))
            # Both modes tie. Lexical ordering must survive pseudonymization.
            archive = self.freeze(source, name="archive-" + str(gold))
            _, rm, rr, rc = self.replay(source, archive, name="replay-" + str(gold))
            live, replay = analyze(m, rows, calls), analyze(rm, rr, rc)
            self.assert_same_statistics(live, replay)
            salt = (archive / "privacy-salt").read_bytes()
            for a, b in zip(live["unlabelled"]["items"], replay["unlabelled"]["items"]):
                self.assertEqual(answer_key(a["old_mode"], [], salt), b["old_mode"])
                self.assertEqual(answer_key(a["new_mode"], [], salt), b["new_mode"])
        # Equal normalized answers must still agree between live and archived arms.
        source, m, rows, calls = self.make_run(gold=False, draws=2, name="equal")
        for call in calls:
            for pred in call["predictions"]:
                pred["answer"] = " YES \n " if call["arm"] == "old" else "yes"
        (source / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
        archive = self.freeze(source, name="equal-archive")
        _, rm, rr, rc = self.replay(source, archive, new="mock:old", name="equal-replay")
        result = analyze(rm, rr, rc)
        self.assertTrue(all(item["old_mode"] == item["new_mode"] for item in result["unlabelled"]["items"]))

    def test_retry_and_off_list_hazards_survive_without_text(self):
        for old in ("retry", "partial", "snake", "case", "extra"):
            source, m, rows, calls = self.make_run(gold=False, draws=2, old="mock:" + old,
                name=old, batch=2, labels=["first label", "second label"])
            archive = self.freeze(source, name="archive-" + old)
            _, rm, rr, rc = self.replay(source, archive, name="replay-" + old)
            self.assert_same_statistics(analyze(m, rows, calls), analyze(rm, rr, rc))

    def test_inputs_mismatch_and_configuration_mismatch_fail_before_provider(self):
        source, _, _, _ = self.make_run(draws=2, labels=["yes", "no"], batch=2)
        archive = self.freeze(source)
        with patch.object(providers, "call", side_effect=AssertionError("No provider calls expected")):
            for extra, message in ((("--batch", "1"), "--batch 2"),
                                   (("--answer", "text", "--labels", str(self.root / "wrong-labels")), "labels")):
                (self.root / "wrong-labels").write_text("no\nyes\n")
                with self.assertRaisesRegex(ValueError, message):
                    self.replay(source, archive, extra=extra)
            rows = [json.loads(line) for line in (source / "inputs.jsonl").read_text().splitlines()]
            rows[0]["input"] += "changed"
            (source / "inputs.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
            with self.assertRaisesRegex(ValueError, "Inputs sha256 differs"):
                self.replay(source, archive)
        self.assertFalse((self.root / "replayed").exists())

    def test_canonical_inputs_and_archive_of_new_arm(self):
        source, _, rows, _ = self.make_run(draws=2, labels=["yes", "no"])
        archive = self.freeze(source, arm="new")
        # JSON property order, whitespace, BOM and CRLF do not change inputs hash.
        (source / "inputs.jsonl").write_text("\ufeff" + "\r\n".join(
            json.dumps(dict(reversed(list(row.items()))), separators=(", ", ": ")) for row in rows))
        _, rm, rr, rc = self.replay(source, archive)
        self.assertEqual(analyze(rm, rr, rc)["models"]["old"], "mock:new")
        self.assertTrue(all(c["predictions"][0]["answer"] == "no" for c in rc if c["arm"] == "old"))

    def test_tool_replay_preserves_choices_and_hazards_without_arguments(self):
        source, m, rows, calls = test_tools.ToolTests.make_run(self, old="flaky", draws=3)
        archive = self.freeze(source, include_raw=True)
        frozen, entries, _ = load_archive(archive)
        self.assertEqual(frozen["tools_sha256"], digest(m["tools"]))
        self.assertNotIn('"arguments"', (archive / "answers.jsonl").read_text())
        _, rm, rr, rc = self.replay(source, archive, new="mock:wrong_tool")
        self.assert_same_statistics(analyze(m, rows, calls), analyze(rm, rr, rc))
        report = self.root / "tool-replay.html"
        build_report(self.root / "replayed", report)
        self.assert_browser_markdown_matches(report)

    def test_cli_integrity_failure_and_legacy_timestamps(self):
        source, m, _, calls = self.make_run(draws=1)
        for call in calls:
            call.pop("timestamp")
            for attempt in call["attempts"]:
                attempt.pop("timestamp")
        (source / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
        archive = self.root / "archive"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["archive", "--runs", str(source), "--arm", "old", "--out", str(archive)]), 0)
            self.assertEqual(main(["archive-verify", str(archive)]), 0)
        self.assertTrue(all(e["timestamp"] == m["created_at"] and e["timestamp_source"] == "run"
                            for e in load_archive(archive)[1]))
        with (archive / "answers.jsonl").open("ab") as stream:
            stream.write(b" ")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["archive-verify", str(archive)]), 2)
        with patch.object(providers, "call", side_effect=AssertionError("No provider calls expected")):
            with self.assertRaises(ValueError):
                self.replay(source, archive)

    def test_incomplete_arm_and_overwrite_are_refused(self):
        source, _, _, calls = self.make_run(draws=2)
        archive = self.freeze(source)
        with self.assertRaisesRegex(ValueError, "must be empty"):
            create_archive(source, "old", archive)
        retained = [c for c in calls if not (c["arm"] == "old" and c["draw"] == 1)]
        (source / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in retained))
        with self.assertRaisesRegex(ValueError, "incomplete arm"):
            self.freeze(source, name="incomplete")

    def test_retired_provider_needs_no_credentials_and_rearchive_keeps_provenance(self):
        source, m, _, _ = self.make_run(gold=False, draws=2)
        m["models"]["old"] = "anthropic:retired-model"
        m["system"] = "PRIVATE_INCUMBENT_SYSTEM"
        m["template"] = "PRIVATE_TEMPLATE {input}"
        (source / "manifest.json").write_text(json.dumps(m))
        archive = self.freeze(source)
        preflight = providers.preflight
        def candidate_only(spec):
            self.assertEqual(spec, "mock:new")
            return preflight(spec)
        with patch.object(providers, "preflight", side_effect=candidate_only) as mocked:
            replayed, rm, rr, rc = self.replay(source, archive)
        mocked.assert_called_once_with("mock:new")
        second = self.freeze(replayed, name="second-archive")
        first_manifest, first_entries, first_salt = load_archive(archive)
        second_manifest, second_entries, second_salt = load_archive(second)
        self.assertEqual(first_salt, second_salt)
        self.assertEqual(first_entries, second_entries)
        for key in ("system_sha256", "template_sha256", "source_version"):
            self.assertEqual(first_manifest[key], second_manifest[key])
        _, sm, sr, sc = self.replay(source, second, name="second-replay")
        self.assert_same_statistics(analyze(rm, rr, rc), analyze(sm, sr, sc))

    def test_report_preserves_hashed_gold_identity_and_salt_edits_fail(self):
        source, _, _, _ = self.make_run(draws=2)
        archive = self.freeze(source)
        replayed, _, _, _ = self.replay(source, archive)
        report = self.root / "hashed-report.html"
        build_report(replayed, report)
        data = json.loads(report.with_suffix(".analysis.json").read_text())
        for item in data["review_queue"]:
            arm = "old" if item["kind"] == "stable_regression" else "new"
            self.assertEqual(item["gold"], item[arm + "_counts"][0]["answer"])
        salt_path = archive / "privacy-salt"
        salt_path.write_bytes(b"x" * 32)
        with self.assertRaisesRegex(ValueError, "salt checksum"):
            verify_archive(archive)

    def test_prompt_changes_require_opt_in_and_are_exported(self):
        source, _, _, _ = self.make_run(draws=2, labels=["yes", "no"])
        archive = self.freeze(source)
        for field, value in (("system", "Different system"), ("template", "Different template: {input}")):
            path = self.root / (field + ".txt")
            path.write_text(value)
            with patch.object(providers, "preflight", side_effect=AssertionError("No preflight expected")):
                with self.assertRaisesRegex(ValueError, "Prompt differs from archive: " + field):
                    self.replay(source, archive, extra=("--" + field, str(path)))
            self.assertFalse((self.root / "replayed").exists())
            replayed, manifest, rows, calls = self.replay(source, archive, name=field,
                extra=("--" + field, str(path), "--allow-prompt-change"))
            changes = manifest["archive_replay"]["prompt_changes"]
            self.assertEqual(set(changes), {field})
            self.assertNotEqual(changes[field]["archive_sha256"], changes[field]["run_sha256"])
            self.assertIn("Prompt differs from archive", analyze(manifest, rows, calls)["warnings"][0])
            report = self.root / (field + ".html")
            build_report(replayed, report)
            for suffix in (".analysis.json", ".decision.json"):
                self.assertEqual(json.loads(report.with_suffix(suffix).read_text())["prompt_changes"], changes)
            self.assertIn("WARNING: Prompt differs from archive", report.with_suffix(".decision.md").read_text())
            self.assert_browser_markdown_matches(report)

    def test_forgery_requires_an_external_head_and_semantics_are_bound(self):
        source, _, _, _ = self.make_run(draws=2, labels=["yes", "no"])
        archive = self.freeze(source)
        original, entries, _ = load_archive(archive)
        expected = original["head_hash"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["archive-verify", str(archive), "--expect-head", expected]), 0)
        self.replay(source, str(archive) + "@" + expected, name="pinned")
        manifest = dict(original)
        manifest["model"] = "mock:forged"
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = digest(manifest)
        (archive / "manifest.json").write_bytes(canonical(manifest) + b"\n")
        # A recomputed checksum alone cannot conceal a semantic manifest edit.
        with self.assertRaisesRegex(ValueError, "hash chain"):
            verify_archive(archive)
        for entry in entries:
            entry["answer"] = "no" if entry["answer"] == "yes" else "yes"
        lines = [canonical(entry) + b"\n" for entry in entries]
        head = manifest_seed(manifest)
        for line in lines:
            head = chain_hash(head, line)
        manifest["head_hash"] = head
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = digest(manifest)
        (archive / "answers.jsonl").write_bytes(b"".join(lines))
        (archive / "manifest.json").write_bytes(canonical(manifest) + b"\n")
        self.assertTrue(verify_archive(archive)["verified"])
        self.replay(source, archive, name="unpinned-forgery")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["archive-verify", str(archive), "--expect-head", expected]), 2)
        with patch.object(providers, "preflight", side_effect=AssertionError("No preflight expected")):
            with self.assertRaisesRegex(ValueError, "head differs from expected"):
                self.replay(source, str(archive) + "@" + expected, name="pinned-forgery")
        self.assertFalse((self.root / "pinned-forgery").exists())

    def test_archived_provenance_draw_override_and_exports(self):
        source, original, _, _ = self.make_run(draws=2, old="mock:mismatch", labels=["yes", "no"])
        archive = self.root / "archive"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["archive", "--runs", str(source), "--arm", "old", "--out", str(archive)]), 0)
        frozen, _, _ = load_archive(archive)
        self.assertIn(frozen["head_hash"], stdout.getvalue())
        self.assertIn("outside the archive", stdout.getvalue())
        self.assertEqual(frozen["source_created_at"], original["created_at"])
        self.assertEqual(frozen["source_audit_id"], original["audit_id"])
        stdout = io.StringIO()
        replayed = self.root / "replayed"
        with redirect_stdout(stdout):
            self.assertEqual(main(["run", "--inputs", str(source / "inputs.jsonl"), "--labels", str(self.root / "run.labels"),
                "--old", "archive:" + str(archive), "--new", "mock:new", "--draws", "9", "--out", str(replayed)]), 0)
        self.assertIn("--draws 9 ignored", stdout.getvalue())
        self.assertIn('"live_calls": 4', stdout.getvalue())
        self.assertIn('"archived_calls": 4', stdout.getvalue())
        manifest, rows, calls = load_run(replayed)
        self.assertEqual(manifest["requested_draws"], 9)
        analysis = analyze(manifest, rows, calls)
        self.assertFalse(any("Served-model mismatch for old" in w for w in analysis["warnings"]))
        self.assertIn(manifest["draws_notice"], analysis["warnings"])
        report = self.root / "provenance.html"
        build_report(replayed, report)
        for suffix in (".analysis.json", ".decision.json"):
            data = json.loads(report.with_suffix(suffix).read_text())
            self.assertEqual(data["archived_old"]["status"], "ARCHIVED")
            for key in ("head_hash", "created_at", "served_models"):
                self.assertEqual(data["archived_old"][key], frozen[key])
        for suffix in (".html", ".decision.md"):
            text = report.with_suffix(suffix).read_text()
            for value in ("ARCHIVED", frozen["head_hash"], frozen["created_at"], *frozen["served_models"], manifest["draws_notice"]):
                self.assertIn(value, text)
        self.assertIn('Old: mock:mismatch (ARCHIVED)', report.with_suffix(".decision.md").read_text())

    def test_error_analysis_matches_live_in_all_answer_modes(self):
        for mode in ("label", "text", "tool"):
            if mode == "tool":
                source, manifest, rows, calls = test_tools.ToolTests.make_run(self, old="flaky", draws=3, name=mode)
                candidate = "mock:wrong_tool"
            else:
                source, manifest, rows, calls = self.make_run(old="mock:retry", draws=3, name=mode,
                    labels=["yes", "no"] if mode == "label" else None)
                candidate = "mock:new"
            # Retain a provider detail that only tool-mode analysis sanitizes.
            call = next(c for c in calls if c["arm"] == "old")
            call["attempts"][0]["error"] = "provider HTTP 429: private detail"
            (source / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
            archive = self.freeze(source, name=mode + "-archive")
            _, replay_manifest, replay_rows, replay_calls = self.replay(source, archive, new=candidate, name=mode + "-replay")
            salt = (archive / "privacy-salt").read_bytes()
            live = private_view(analyze(manifest, rows, calls), salt=salt)
            replay = self.without_archive_metadata(analyze(replay_manifest, replay_rows, replay_calls))
            replay.pop("answers_hashed")
            replay["created_at"] = live["created_at"]
            self.assertTrue(any("old call errors:" in w for w in live["warnings"]))
            self.assertEqual(live, replay, mode)
            errors = [a["error"] for entry in load_archive(archive)[1] for a in entry["attempts"] if a["error"]]
            self.assertTrue(errors)
            if mode == "tool":
                self.assertNotIn("private detail", " ".join(errors))
            else:
                self.assertIn("provider HTTP 429: private detail", errors)

    def test_archived_failures_do_not_request_live_credentials(self):
        source, manifest, rows, calls = self.make_run(old="mock:fail", draws=1, batch=2,
            labels=["yes", "no"], stopped=True)
        self.assertTrue(any("check credentials" in w for w in analyze(manifest, rows, calls)["warnings"]))
        archive = self.freeze(source)
        _, manifest, rows, calls = self.replay(source, archive)
        warnings = analyze(manifest, rows, calls)["warnings"]
        self.assertFalse(any("check credentials" in w for w in warnings))
        self.assertTrue(any("old call errors:" in w for w in warnings))

    def assert_browser_markdown_matches(self, report):
        if not shutil.which("node"):
            self.skipTest("Node is unavailable for browser Markdown export verification")
        record = json.loads(report.with_suffix(".decision.json").read_text())
        script = report.read_text().split("function markdown(){", 1)[1].split("\nbyId('md-export')", 1)[0]
        code = "function markdown(){" + script
        harness = """const fs=require('fs'),vm=require('vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context={record:input.record,toolHazards:input.toolHazards};
vm.createContext(context);
process.stdout.write(vm.runInContext(input.code+';markdown()',context));"""
        result = subprocess.run(["node", "-e", harness],
            input=json.dumps({"record": record, "code": code,
                              "toolHazards": [[key, key.replace("_", " ")] for key in TOOL_HAZARDS]}), text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, markdown(record))

    def test_invalid_numbers_and_write_failures_leave_no_partial_archive(self):
        source, _, rows, _ = self.make_run(draws=2)
        original = (source / "inputs.jsonl").read_text()
        for value in (float("nan"), float("inf"), float("-inf")):
            rows[0]["metadata"] = {"score": value}
            (source / "inputs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaisesRegex(ValueError, "Input line 1: non-finite"):
                self.freeze(source)
            self.assertFalse((self.root / "archive").exists())
        (source / "inputs.jsonl").write_text(original)
        # Later serialization failures are also discovered before any output files.
        with patch("migration_check.archive.text_digest", side_effect=ValueError("bad prompt digest")):
            with self.assertRaisesRegex(ValueError, "bad prompt digest"):
                self.freeze(source)
        self.assertFalse((self.root / "archive").exists())
        with patch("migration_check.archive.load_archive", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.freeze(source)
        self.assertFalse((self.root / "archive").exists())
        self.assertFalse(list(self.root.glob(".migration-archive-*")))
        (self.root / "archive").mkdir()
        self.assertTrue(verify_archive(self.freeze(source))["verified"])


if __name__ == "__main__":
    unittest.main()
