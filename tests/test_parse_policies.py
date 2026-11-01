"""Parse policies: strict parse rates, strict and parsed-only accuracy, and named recovery policies.

Every run here is synthetic and offline: hand-written captures or `mock:` providers.
"""

import json
import unittest

import test_archive
import test_migration_check as base
from migration_check.analysis import analyze, load_run
from migration_check.report import build_report, policy_table, self_correction_text

LABELS = ["date", "travel_notification", "mpg", "restaurant_reviews", "smart_home", "reminder", "todo_list"]
GOLD = ["date", "travel_notification", "mpg", "restaurant_reviews", "smart_home", "reminder"]


def keyed(values):
    return json.dumps({str(i + 1): value for i, value in enumerate(values)})


FIRST = GOLD[:5] + ["todo_list"]
# The live demo's shape: answer, a "Wait" note, then a corrected second object.
SELF_CORRECTED = keyed(FIRST) + "\n\nWait, message 6 is about reminders.\n\n" + keyed(GOLD)


def attempt(raw, parsed, error=None, objects=None):
    record = {"attempt": 0, "raw_text": raw, "served_models": ["served"], "usage": {}, "error": error,
              "refusal": False, "truncated": error == "truncated", "filtered": False,
              "parsed_answers": parsed, "parse_failures": sum(value is None for value in parsed),
              "json_objects": objects if objects is not None else raw.count('{"1"')}
    if record["json_objects"] >= 2:
        record.update(self_correction=True, recovered_answers=list(GOLD))
    return record


class ParsePolicyTests(unittest.TestCase):
    setUp = base.OfflineTests.setUp
    make_run = base.OfflineTests.make_run
    assert_browser_markdown_matches = test_archive.ArchiveTests.assert_browser_markdown_matches

    def write_run(self, new_calls, name="demo", gold=True, old_calls=None):
        """A hand-written capture: 6 items, batch 6, 2 draws, 2 retries."""
        directory = self.root / name
        directory.mkdir()
        rows = [{"id": f"c{i}", "input": f"message {i}"} for i in range(6)]
        if gold:
            for row, label in zip(rows, GOLD):
                row["gold"] = label
        manifest = {"schema_version": 1, "version": "test", "audit_id": name, "created_at": "2026-11-01T00:00:00+00:00",
                    "models": {"old": "claude-cli:served", "new": "claude-cli:served"}, "draws": 2, "batch": 6,
                    "workers": 1, "retries": 2, "timeout": 60, "labels": LABELS, "n_items": 6, "system": "s",
                    "template": None, "complete": True, "answer": "text", "tools": []}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "inputs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        (directory / "privacy-salt").write_bytes(b"\x01" * 32)
        clean = [[attempt("```json\n" + keyed(GOLD) + "\n```", list(GOLD))] for _ in range(2)]
        lines = []
        for arm, per_draw in (("old", old_calls or clean), ("new", new_calls)):
            for draw, attempts in enumerate(per_draw):
                final = attempts[-1]["parsed_answers"]
                lines.append({"arm": arm, "requested_model": "claude-cli:served", "draw": draw, "batch": 0,
                              "item_ids": [row["id"] for row in rows], "attempts": attempts,
                              "retries": len(attempts) - 1, "error": attempts[-1]["error"],
                              "refusal": False, "truncated": attempts[-1]["error"] == "truncated", "filtered": False,
                              "predictions": [{"id": row["id"], "answer": value, "pattern": None}
                                              for row, value in zip(rows, final)]})
        (directory / "calls.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
        return directory

    def demo_run(self, **kwargs):
        """The 6-message live demo, offline: every new reply self-corrects except a last retry that parsed."""
        missing = [None] * 6
        draw0 = [attempt(SELF_CORRECTED, missing, "unparseable") for _ in range(3)]
        draw1 = [attempt(SELF_CORRECTED, missing, "unparseable") for _ in range(2)]
        draw1.append(attempt(keyed(FIRST), list(FIRST)))
        return self.write_run([draw0, draw1], **kwargs)

    def test_demo_shape_reports_every_measure_with_its_denominator(self):
        directory = self.demo_run()
        analysis = analyze(*load_run(directory))
        policies = analysis["parse_policies"]
        self.assertEqual(policies["applicable"], ["strict", "strict_first_attempt", "last_object", "object_merge"])
        self.assertEqual(policies["retries"], 2)
        new, old = policies["arms"]["new"], policies["arms"]["old"]
        self.assertEqual(new["strict"], {"calls": 2, "calls_parsed": 1, "answers": 12, "parsed": 6, "gold": 12,
                                         "parsed_gold": 6, "correct": 5, "accuracy": 5 / 12,
                                         "parsed_accuracy": 5 / 6, "available": True})
        # Strict accuracy is exactly the headline number; the parsed-only rate has its own denominator.
        self.assertEqual(new["strict"]["accuracy"], analysis["gold"]["new_accuracy"])
        self.assertEqual(old["strict"]["accuracy"], analysis["gold"]["old_accuracy"])
        self.assertEqual((new["strict_first_attempt"]["parsed"], new["strict_first_attempt"]["correct"]), (0, 0))
        # Recovery reads each call's first reply, so draw 1 never reaches the retry that answered todo_list.
        for policy in ("last_object", "object_merge"):
            self.assertEqual((new[policy]["calls_parsed"], new[policy]["parsed"], new[policy]["correct"],
                              new[policy]["accuracy"]), (2, 12, 12, 1.0), policy)
        for policy in policies["applicable"]:
            self.assertEqual((old[policy]["parsed"], old[policy]["correct"]), (12, 12), policy)

        outcomes = analysis["hazard_summary"]["new"]["self_correction_outcomes"]
        self.assertEqual((outcomes["calls"], outcomes["answers"], outcomes["gold"], outcomes["retry_parsed_calls"],
                          outcomes["unparsed_calls"], outcomes["other_failure_calls"]), (2, 12, 12, 1, 1, 0))
        self.assertEqual((outcomes["policies"]["strict"]["parsed"], outcomes["policies"]["strict"]["correct"]), (6, 5))
        self.assertEqual(self_correction_text(analysis["hazard_summary"]["new"]),
                         "Self-correction: 2 calls answered, then wrote a second JSON answer in the same reply. "
                         "A strict parser expects one JSON object and reads no answer from such a reply. "
                         "As recorded, 1 call parsed on a retry and 1 call ended with no parseable reply. "
                         "Strict, as recorded: 6 of 12 answers in these calls parse and 5 are correct. "
                         "Last-object recovery: 12 of 12 parse and 12 are correct. "
                         "Object-merge recovery: 12 of 12 parse and 12 are correct.")

        build_report(directory, self.root / "report.html")
        record = (self.root / "report.decision.md").read_text()
        for row in ("| Measure (each model: 2 calls, 12 answers, all with gold) | old | new |",
                    "| Strict parse: calls with every answer parsed | 2 of 2 | 1 of 2 |",
                    "| Strict parse: answers parsed | 12 of 12 | 6 of 12 |",
                    "| Strict accuracy: correct of all gold answers (a missing or off-list answer counts wrong) "
                    "| 12 of 12 (100.0%) | 5 of 12 (41.7%) |",
                    "| Accuracy among strictly parsed answers only | 12 of 12 (100.0%) | 5 of 6 (83.3%) |",
                    "| Accuracy, strict, first reply only (no retries) | 12 of 12 (100.0%) | 0 of 12 (0.0%) |",
                    "| Accuracy, last-object recovery | 12 of 12 (100.0%) | 12 of 12 (100.0%) |",
                    "| Accuracy, object-merge recovery (later answers win) | 12 of 12 (100.0%) | 12 of 12 (100.0%) |",
                    "Strict accuracy on gold: old 100.0%, new 41.7% (D* -58.3 percentage points, new minus old).",
                    "If a recovery row closes most of a gap"):
            self.assertIn(row, record)
        # The old run-on sentence is gone.
        self.assertNotIn("whose last attempt was one", record)
        analysis_file = json.loads((self.root / "report.analysis.json").read_text())
        self.assertEqual(analysis_file["parse_policies"], json.loads(json.dumps(policies)))
        self.assertEqual(analysis_file["parse_policy_table"]["rows"][2], [
            "Strict accuracy: correct of all gold answers (a missing or off-list answer counts wrong)",
            "12 of 12 (100.0%)", "5 of 12 (41.7%)"])
        decision = json.loads((self.root / "report.decision.json").read_text())
        self.assertEqual(decision["parse_policies"], analysis_file["parse_policies"])
        page = (self.root / "report.html").read_text()
        self.assertIn("parse_policy_table", page)
        self.assertIn("Parse success and accuracy by parse policy", page)
        self.assert_browser_markdown_matches(self.root / "report.html")

    def test_last_object_and_merge_differ_and_off_list_recoveries_count_wrong(self):
        missing = [None] * 6
        partial_fix = keyed(FIRST) + " Wait, message 6 is REMINDER. " + json.dumps({"6": "REMINDER"})
        draw0 = [attempt(partial_fix, missing, "unparseable", objects=2)]
        # A truncated reply is a final outcome; recovery never reads its objects.
        draw1 = [attempt(SELF_CORRECTED, missing, "truncated")]
        directory = self.write_run([draw0, draw1])
        new = analyze(*load_run(directory))["parse_policies"]["arms"]["new"]
        # Last object holds only message 6, off-list by case: 1 parsed, 0 correct.
        self.assertEqual((new["last_object"]["parsed"], new["last_object"]["correct"]), (1, 0))
        self.assertEqual(new["last_object"]["calls_parsed"], 0)
        # Merge keeps messages 1-5 from the first object and REMINDER for 6: 6 parsed, 5 correct.
        self.assertEqual((new["object_merge"]["parsed"], new["object_merge"]["correct"]), (6, 5))
        self.assertEqual(new["object_merge"]["parsed_accuracy"], 5 / 6)
        self.assertEqual(new["object_merge"]["accuracy"], 5 / 12)
        self.assertEqual(new["strict"]["parsed_accuracy"], None)
        table = policy_table({"parse_policies": analyze(*load_run(directory))["parse_policies"]})
        self.assertEqual(table["rows"][3], ["Accuracy among strictly parsed answers only", "12 of 12 (100.0%)", "none"])

    def test_first_reply_only_drops_a_retry_that_parsed(self):
        retry = [attempt("not json", [None] * 6, "unparseable", objects=0), attempt(keyed(GOLD), list(GOLD))]
        directory = self.write_run([retry, [attempt(keyed(GOLD), list(GOLD))]])
        new = analyze(*load_run(directory))["parse_policies"]["arms"]["new"]
        self.assertEqual((new["strict"]["correct"], new["strict_first_attempt"]["correct"]), (12, 6))
        # Nothing to recover in a reply with no object, so recovery follows the recorded retry.
        self.assertEqual(new["last_object"]["correct"], 12)

    def test_without_gold_the_table_reports_parse_rates_only(self):
        directory = self.demo_run(gold=False)
        analysis = analyze(*load_run(directory))
        new = analysis["parse_policies"]["arms"]["new"]
        self.assertEqual((new["strict"]["gold"], new["strict"]["accuracy"], new["strict"]["parsed_accuracy"]),
                         (0, None, None))
        table = policy_table(analysis)
        self.assertEqual([row[0] for row in table["rows"]], [
            "Strict parse: calls with every answer parsed", "Strict parse: answers parsed",
            "Answers parsed, strict, first reply only (no retries)", "Answers parsed, last-object recovery",
            "Answers parsed, object-merge recovery (later answers win)"])
        self.assertEqual(table["rows"][3][1:], ["12 of 12", "12 of 12"])
        self.assertEqual(self_correction_text(analysis["hazard_summary"]["new"]).split(". ")[-1],
                         "Object-merge recovery: 12 of 12 parse.")

    def test_unbatched_runs_have_no_recovery_policies(self):
        _, manifest, rows, calls = self.make_run(batch=1, draws=2, labels=["yes", "no"])
        policies = analyze(manifest, rows, calls)["parse_policies"]
        self.assertEqual(policies["applicable"], ["strict", "strict_first_attempt"])
        self.assertEqual(set(policies["not_applicable"]), {"last_object", "object_merge"})
        table = policy_table({"parse_policies": policies})
        self.assertFalse(any("recovery" in row[0] for row in table["rows"]))
        self.assertEqual(len(table["notes"]), 1)

    def test_archive_without_raw_text_says_not_recorded_instead_of_guessing(self):
        for include_raw in (False, True):
            with self.subTest(include_raw=include_raw):
                self.setUp()
                source, manifest, rows, calls = self.make_run(old="mock:self-correct", new="mock:old", batch=2,
                                                              draws=2, labels=["yes", "no"], name="sc-source")
                live = analyze(manifest, rows, calls)["parse_policies"]["arms"]["old"]
                archive = test_archive.ArchiveTests.freeze(self, source, name="sc-archive", include_raw=include_raw)
                directory, rm, rr, rc = test_archive.ArchiveTests.replay(self, source, archive, new="mock:old")
                replay = analyze(rm, rr, rc)["parse_policies"]["arms"]["old"]
                for policy in ("strict", "strict_first_attempt", "object_merge"):
                    self.assertEqual(replay[policy], live[policy], policy)
                if include_raw:
                    self.assertEqual(replay["last_object"], live["last_object"])
                    continue
                # The archive keeps merged answers but not the reply text that last-object needs.
                self.assertEqual(replay["last_object"]["available"], False)
                build_report(directory, self.root / "archived.html")
                record = (self.root / "archived.decision.md").read_text()
                self.assertIn("| Accuracy, last-object recovery | not recorded | 2 of 4 (50.0%) |", record)
                self.assertIn("Last-object recovery: not recorded (the archive holds no reply text for this arm).",
                              record)
                self.assert_browser_markdown_matches(self.root / "archived.html")


if __name__ == "__main__":
    unittest.main()
