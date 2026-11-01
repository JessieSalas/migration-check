"""Self-correction: a batched reply that holds a second, corrected JSON object."""

import json
import unittest

import test_archive
import test_migration_check as base
from migration_check.analysis import analyze, load_run
from migration_check.archive import canonical, chain_hash, digest, manifest_seed
from migration_check.protocol import json_objects, parse_keyed, recover_keyed
from migration_check.report import build_report, self_correction_text

TWO_OBJECTS = '{"1": "a", "2": "b"} Wait, message 2 is c. {"1": "a", "2": "c"}'
# Draw 1's second object corrects only message 2, so last-object recovery reads 1 of its 2 answers.
SENTENCE = ("Self-correction: 2 calls answered, then wrote a second JSON answer in the same reply. "
            "A strict parser expects one JSON object and reads no answer from such a reply. "
            "As recorded, 2 calls ended with no parseable reply. "
            "Strict, as recorded: 0 of 4 answers in these calls parse and 0 are correct. "
            "Last-object recovery: 3 of 4 parse and 3 are correct. "
            "Object-merge recovery: 4 of 4 parse and 4 are correct.")


class ScannerTests(unittest.TestCase):
    def test_counts_top_level_objects_only(self):
        self.assertEqual(json_objects(TWO_OBJECTS), [{"1": "a", "2": "b"}, {"1": "a", "2": "c"}])
        self.assertEqual(len(json_objects('```json\n{"1": "a", "2": {"x": {"y": 1}}}\n```')), 1)
        self.assertEqual(json_objects("no answer {here} at all { nor here"), [])
        self.assertEqual(json_objects(""), [])
        # Arrays and broken fragments are not objects; a later valid object still counts.
        self.assertEqual(json_objects('[1, 2] {"1": oops} {"1": "a"}'), [{"1": "a"}])

    def test_merge_later_keys_win_including_partial_second_object(self):
        self.assertEqual(recover_keyed(TWO_OBJECTS, 2), (2, ["a", "c"]))
        self.assertEqual(recover_keyed('{"1": "a", "2": "b", "3": "x"} Wait. {"2": "c"}', 3), (2, ["a", "c", "x"]))
        self.assertEqual(recover_keyed('{"1": "a"} then {"1": "b"} and {"1": "d"}', 1), (3, ["d"]))
        # Same per-item validity rules as parse_keyed: nulls, containers and blanks are missing.
        self.assertEqual(recover_keyed('{"1": " a ", "2": null, "3": ["x"], "4": " "}', 4), (1, ["a", None, None, None]))
        self.assertEqual(recover_keyed("garbage", 2), (0, [None, None]))

    def test_shapes_that_are_not_a_second_answer_are_not_counted(self):
        # An array of per-item objects is a different format, not a correction.
        self.assertEqual(recover_keyed('[{"1": "a"}, {"2": "b"}]', 2), (0, [None, None]))
        self.assertEqual(recover_keyed('[{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]', 2), (0, [None, None]))
        # A malformed outer object is one failed span; its nested objects are not top level.
        self.assertEqual(recover_keyed('{"1": {"x": 1}, "2": {"y": 2},}', 2), (0, [None, None]))
        # An empty {} or a format example in prose is not an answer object.
        self.assertEqual(recover_keyed('{"1": "a", "2": "b"} Note: {} is empty.', 2), (1, ["a", "b"]))
        self.assertEqual(recover_keyed('```json\n{"1": "a", "2": "b"}\n```\nFormat: {"key": "label"}', 2),
                         (1, ["a", "b"]))
        # An unclosed brace in prose does not hide the answers after it.
        self.assertEqual(recover_keyed('Use { here. {"1": "a"} Wait. {"1": "b"}', 1), (2, ["b"]))
        # Quotes and braces inside strings do not end an object.
        self.assertEqual(len(json_objects('{"1": "a } \\" ]"} then {"1": "b"}')), 2)

    def test_a_later_null_or_blank_never_erases_an_answer(self):
        self.assertEqual(recover_keyed('{"1": "x", "2": "y"} Wait. {"1": "z", "2": null}', 2), (2, ["z", "y"]))
        self.assertEqual(recover_keyed('{"1": "x"} Wait. {"1": " ", "2": ["q"]}', 2), (2, ["x", None]))

    def test_scanner_is_fast_on_degenerate_replies(self):
        import time
        for raw in ('{"a":"' + "{" * 200000, "{" * 200000, "{\n" * 200000, "[" * 200000 + '{"1": "a"}'):
            started = time.monotonic()
            json_objects(raw)
            # Measured near 0.3 s; quadratic decoding took 25 to 52 s on these.
            self.assertLess(time.monotonic() - started, 5)

    def test_parse_keyed_still_fails_a_two_object_reply(self):
        self.assertEqual(parse_keyed(TWO_OBJECTS, 2), [None, None])
        self.assertEqual(parse_keyed('{"1": "a"}{"1": "b"}', 1), [None])
        self.assertEqual(parse_keyed('{"1": "a", "2": "b"}', 2), ["a", "b"])


class SelfCorrectionRunTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp
    make_run = base.OfflineTests.make_run
    assert_browser_markdown_matches = test_archive.ArchiveTests.assert_browser_markdown_matches

    def self_correct_run(self):
        return self.make_run(old="mock:old", new="mock:self-correct", batch=2, draws=2, labels=["yes", "no"])

    def test_runner_records_objects_and_recovery_without_changing_predictions(self):
        directory, manifest, rows, calls = self.self_correct_run()
        new_calls = sorted((c for c in calls if c["arm"] == "new"), key=lambda c: c["draw"])
        for call in new_calls:
            self.assertEqual(call["retries"], 2)
            self.assertEqual(call["error"], "unparseable")
            self.assertEqual([p["answer"] for p in call["predictions"]], [None, None])
            for attempt in call["attempts"]:
                self.assertEqual(attempt["json_objects"], 2)
                self.assertIs(attempt["self_correction"], True)
                # Draw 1 corrects only message 2; merging keeps message 1 from the first object.
                self.assertEqual(attempt["recovered_answers"], ["yes", "no"])
                self.assertEqual(attempt["parsed_answers"], [None, None])
        self.assertIn('"2": "no"}', new_calls[0]["attempts"][0]["raw_text"])
        self.assertTrue(new_calls[1]["attempts"][0]["raw_text"].endswith('{"2": "no"}'))
        for call in (c for c in calls if c["arm"] == "old"):
            for attempt in call["attempts"]:
                self.assertEqual(attempt["json_objects"], 1)
                self.assertNotIn("self_correction", attempt)
                self.assertNotIn("recovered_answers", attempt)

        analysis = analyze(manifest, rows, calls)
        new, old = analysis["hazard_summary"]["new"], analysis["hazard_summary"]["old"]
        self.assertEqual(new["self_correction"], 2)
        self.assertEqual(old["self_correction"], 0)
        # Calls are not items, so self-correction is its own field, never a pattern.
        self.assertNotIn("self_correction", new["patterns"])
        self.assertEqual(new["dominant_pattern"], "parse_failure")
        self.assertEqual(new["self_correction_recoverable_items"],
                         {"calls": 2, "items": 4, "on_list": 4, "gold_items": 4, "correct": 4,
                          "retry_parsed": 0, "other_failure": 0, "unrecorded": 0})
        self.assertEqual(old["self_correction_recoverable_items"]["calls"], 0)
        self.assertEqual([h["self_correction"] for h in analysis["hazards"] if h["arm"] == "new"], [1, 1])
        # Accuracy still scores the failed parses as wrong.
        self.assertEqual(new["parse_failures"], 4)
        self.assertEqual(analysis["gold"]["new_accuracy"], 0.0)

        result = build_report(directory, self.root / "report.html")
        record = (self.root / "report.decision.md").read_text()
        self.assertIn("new: " + SENTENCE, record)
        self.assertNotIn("old: Self-correction", record)
        page = (self.root / "report.html").read_text()
        self.assertIn(json.dumps({"new": SENTENCE}), page)
        self.assertEqual(result["queued_items"], len(json.loads(
            (self.root / "report.decision.json").read_text())["items"]))
        self.assert_browser_markdown_matches(self.root / "report.html")

    def test_partial_recovery_and_off_list_answers_are_counted_honestly(self):
        directory, manifest, rows, calls = self.self_correct_run()
        call = next(c for c in calls if c["arm"] == "new" and c["draw"] == 0)
        call["attempts"][-1]["recovered_answers"] = ["YES", "no"]
        earlier = next(c for c in calls if c["arm"] == "new" and c["draw"] == 1)
        # A retry that parsed: the call still had a self-correction, but its last attempt was not one.
        earlier["attempts"][-1].pop("self_correction")
        earlier["attempts"][-1].update(error=None, parse_failures=0, json_objects=1, parsed_answers=["yes", "no"])
        earlier["error"] = None
        for prediction, answer in zip(earlier["predictions"], ["yes", "no"]):
            prediction.update(answer=answer, pattern=None)
        recovery = analyze(manifest, rows, calls)["hazard_summary"]["new"]
        self.assertEqual(recovery["self_correction"], 2)
        self.assertEqual(recovery["self_correction_recoverable_items"],
                         {"calls": 1, "items": 2, "on_list": 1, "gold_items": 1, "correct": 1,
                          "retry_parsed": 1, "other_failure": 0, "unrecorded": 0})
        # The policies read the stored reply text, not the descriptive recovered_answers field.
        # Last-object recovery stops at draw 1's first reply, whose last object answers 1 of 2.
        self.assertTrue(self_correction_text(recovery).endswith(
            "As recorded, 1 call parsed on a retry and 1 call ended with no parseable reply. "
            "Strict, as recorded: 2 of 4 answers in these calls parse and 2 are correct. "
            "Last-object recovery: 3 of 4 parse and 3 are correct. "
            "Object-merge recovery: 4 of 4 parse and 4 are correct."), self_correction_text(recovery))

    def test_a_retry_that_fails_another_way_is_not_reported_as_parsed(self):
        directory, manifest, rows, calls = self.self_correct_run()
        for call in (c for c in calls if c["arm"] == "new"):
            call["attempts"][-1].pop("self_correction")
            call["attempts"][-1].update(error="claude-cli error (exit 1): overloaded", parse_failures=2)
        new = analyze(manifest, rows, calls)["hazard_summary"]["new"]
        self.assertEqual(new["self_correction"], 2)
        self.assertEqual((new["self_correction_recoverable_items"]["retry_parsed"],
                          new["self_correction_recoverable_items"]["other_failure"]), (0, 2))
        text = self_correction_text(new)
        self.assertNotIn("parsed on a retry", text)
        self.assertIn("As recorded, 2 calls ended in another failure (see the call errors).", text)
        last_new = [c for c in calls if c["arm"] == "new"][-1]
        last_new["attempts"][-1].update(error=None, parse_failures=0)
        for prediction, answer in zip(last_new["predictions"], ["yes", "no"]):
            prediction.update(answer=answer, pattern=None)
        mixed = self_correction_text(analyze(manifest, rows, calls)["hazard_summary"]["new"])
        self.assertIn("As recorded, 1 call parsed on a retry and 1 call ended in another failure "
                      "(see the call errors).", mixed)

    def test_old_capture_without_new_keys_still_analyses_and_reports(self):
        directory, *_ = self.self_correct_run()
        calls_path = directory / "calls.jsonl"
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
        for call in calls:
            for attempt in call["attempts"]:
                for key in ("json_objects", "self_correction", "recovered_answers"):
                    attempt.pop(key, None)
        calls_path.write_text("".join(json.dumps(c) + "\n" for c in calls))
        manifest, rows, calls = load_run(directory)
        analysis = analyze(manifest, rows, calls)
        for arm in ("old", "new"):
            self.assertEqual(analysis["hazard_summary"][arm]["self_correction"], 0)
            self.assertNotIn("self_correction", analysis["hazard_summary"][arm]["patterns"])
        build_report(directory, self.root / "old.html")
        self.assertNotIn("Self-correction", (self.root / "old.decision.md").read_text())
        self.assertIn('"self_correction_notes": {}', (self.root / "old.analysis.json").read_text())
        self.assert_browser_markdown_matches(self.root / "old.html")

    def archived_self_correct_run(self, include_raw):
        source, manifest, rows, calls = self.make_run(old="mock:self-correct", new="mock:old", batch=2, draws=2,
                                                      labels=["yes", "no"], name="sc-source")
        archive = test_archive.ArchiveTests.freeze(self, source, name="sc-archive", include_raw=include_raw)
        return source, analyze(manifest, rows, calls), archive

    def test_archived_arm_keeps_its_self_correction_counts(self):
        for include_raw in (False, True):
            with self.subTest(include_raw=include_raw):
                self.setUp()
                source, live, archive = self.archived_self_correct_run(include_raw)
                entry = json.loads((archive / "answers.jsonl").read_text().splitlines()[0])
                self.assertEqual({a["json_objects"] for a in entry["attempts"]}, {2})
                _, rm, rr, rc = test_archive.ArchiveTests.replay(self, source, archive, new="mock:old")
                replay = analyze(rm, rr, rc)
                self.assertEqual(replay["hazard_summary"]["old"]["self_correction"], 2)
                self.assertEqual(replay["hazard_summary"]["old"]["self_correction_recoverable_items"],
                                 live["hazard_summary"]["old"]["self_correction_recoverable_items"])
                self.assertEqual([h["self_correction"] for h in replay["hazards"] if h["arm"] == "old"], [1, 1])

    def strip_and_reseal(self, archive):
        """An archive made before v0.4.1: attempts carry no self-correction keys."""
        lines = []
        for line in (archive / "answers.jsonl").read_bytes().splitlines():
            entry = json.loads(line)
            for attempt in entry["attempts"]:
                for key in ("json_objects", "self_correction", "recovered_answers"):
                    attempt.pop(key, None)
            lines.append(canonical(entry) + b"\n")
        (archive / "answers.jsonl").write_bytes(b"".join(lines))
        manifest = json.loads((archive / "manifest.json").read_text())
        manifest.pop("manifest_sha256")
        manifest.pop("head_hash")
        head = manifest_seed(manifest)
        for line in lines:
            head = chain_hash(head, line)
        manifest["head_hash"] = head
        manifest["manifest_sha256"] = digest(manifest)
        (archive / "manifest.json").write_bytes(canonical(manifest) + b"\n")

    def test_older_archive_recomputes_from_raw_or_says_not_recorded(self):
        source, live, archive = self.archived_self_correct_run(include_raw=True)
        self.strip_and_reseal(archive)
        _, rm, rr, rc = test_archive.ArchiveTests.replay(self, source, archive, new="mock:old", name="raw-replay")
        old = analyze(rm, rr, rc)["hazard_summary"]["old"]
        self.assertEqual(old["self_correction"], 2)
        self.assertEqual(old["self_correction_recoverable_items"],
                         live["hazard_summary"]["old"]["self_correction_recoverable_items"])

        self.setUp()
        source, live, archive = self.archived_self_correct_run(include_raw=False)
        self.strip_and_reseal(archive)
        directory, rm, rr, rc = test_archive.ArchiveTests.replay(self, source, archive, new="mock:old")
        analysis = analyze(rm, rr, rc)
        self.assertIsNone(analysis["hazard_summary"]["old"]["self_correction"])
        self.assertEqual([h["self_correction"] for h in analysis["hazards"] if h["arm"] == "old"], [None, None])
        build_report(directory, self.root / "unrecorded.html")
        record = (self.root / "unrecorded.decision.md").read_text()
        self.assertIn("old: Self-correction: not recorded for the archived arm", record)
        self.assert_browser_markdown_matches(self.root / "unrecorded.html")


if __name__ == "__main__":
    unittest.main()
