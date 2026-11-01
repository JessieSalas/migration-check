"""v0.4.4: accuracy among on-list answers, where the hazards are, and the generated decision summary.

Every run here is synthetic and offline: hand-written captures in the shape of a live run.
"""

import json
import unittest

import test_parse_policies as pp
from migration_check.analysis import analyze, load_run, paired_interval
from migration_check.report import build_report, concentration_text, decision_summary, policy_table

GOLD = pp.GOLD
OFF_LIST = [label.upper() for label in GOLD]  # A case change: parses, but no allowed label is spelled that way.
FORMAT = "the drop is an output-format change, not a loss of task accuracy on the answers that follow the format"
UNDETECTED = ("the drop is mostly an output-format change; no loss of task accuracy on the answers that follow the "
              "format is detected, but a loss of up to 49.3 points is not ruled out")
AUTHORITATIVE = "Strict accuracy stays authoritative while the output contract is unchanged"


def clean(values):
    return pp.attempt(pp.keyed(values), list(values))


class DecisionSummaryTests(unittest.TestCase):
    setUp = pp.ParsePolicyTests.setUp
    write_run = pp.ParsePolicyTests.write_run
    assert_browser_markdown_matches = pp.ParsePolicyTests.assert_browser_markdown_matches

    def off_list_run(self, second, name="off"):
        """New answers draw 0 entirely off-list (every answer upper-cased) and draw 1 with `second`."""
        return self.write_run([[clean(OFF_LIST)], [clean(second)]], name=name)

    def test_on_list_row_has_its_own_denominator_where_the_parsed_row_cannot(self):
        directory = self.off_list_run(GOLD[:5] + ["todo_list"])
        analysis = analyze(*load_run(directory))
        new = analysis["parse_policies"]["arms"]["new"]["strict"]
        # Off-list answers parse, so the parsed-only rate equals strict accuracy and proves nothing.
        self.assertEqual((new["parsed"], new["parsed_gold"], new["correct"]), (12, 12, 5))
        self.assertEqual(new["parsed_accuracy"], new["accuracy"])
        self.assertEqual((new["on_list"], new["on_list_gold"], new["on_list_accuracy"]), (6, 6, 5 / 6))
        rows = {row[0]: row[1:] for row in policy_table(analysis)["rows"]}
        self.assertEqual(rows["Accuracy among strictly parsed answers only"], ["12 of 12 (100.0%)", "5 of 12 (41.7%)"])
        self.assertEqual(rows["Accuracy among on-list answers only (parsed and spelled as an allowed label)"],
                         ["12 of 12 (100.0%)", "5 of 6 (83.3%)"])
        # The gold block carries the same counts, with an item-clustered interval for the difference.
        on_list = analysis["gold"]["on_list"]
        self.assertEqual((on_list["new"]["correct"], on_list["new"]["answers"]), (5, 6))
        self.assertEqual((on_list["old"]["correct"], on_list["old"]["answers"]), (12, 12))
        low, high = on_list["ci95"]
        self.assertLess(low, on_list["difference"])
        self.assertLess(on_list["difference"], high)

    def test_on_list_row_is_absent_without_a_label_list(self):
        _, manifest, rows, calls = pp.base.OfflineTests.make_run(self, batch=1, draws=2)
        table = policy_table(analyze(manifest, rows, calls))
        self.assertFalse(any(row[0].startswith("Accuracy among on-list") for row in table["rows"]))

    def test_where_the_hazards_are_names_the_calls(self):
        directory = self.off_list_run(GOLD[:5] + ["todo_list"])
        analysis = analyze(*load_run(directory))
        self.assertEqual(analysis["hazard_concentration"]["new"]["off_list_calls"], 1)
        self.assertEqual(concentration_text(analysis),
                         "Where the hazards are. Old: no off-list or missing answers. New: 6 off-list answers "
                         "(case_change 6), all in 1 of 2 calls, every answer in that call.")
        # One off-list answer in one call of six.
        partial = list(GOLD)
        partial[2] = "MPG"
        directory = self.write_run([[clean(partial)], [clean(GOLD)]], name="partial")
        self.assertEqual(concentration_text(analyze(*load_run(directory))),
                         "Where the hazards are. Old: no off-list or missing answers. New: 1 off-list answer "
                         "(case_change 1), all in 1 of 2 calls, at most 1 of 6 answers in one call.")
        # Missing answers are counted the same way.
        missing = pp.attempt("not json", [None] * 6, "unparseable", objects=0)
        directory = self.write_run([[missing, missing, missing], [clean(GOLD)]], name="missing")
        self.assertIn("New: 6 missing or unparseable answers, all in 1 of 2 calls, every answer in that call.",
                      concentration_text(analyze(*load_run(directory))))

    def test_summary_says_format_not_capability_only_when_on_list_accuracy_holds(self):
        directory = self.off_list_run(GOLD[:5] + ["todo_list"])
        build_report(directory, self.root / "format.html")
        record = (self.root / "format.decision.md").read_text()
        summary = ("New loses 58.3 points of strict accuracy on gold (old 100.0%, new 41.7%). All answers parse; "
                   "6 of 12 use labels outside the allowed set, in 1 of 2 calls. Among on-list answers, accuracy "
                   "is 83.3% (old 100.0%; the 95% interval of the difference, -49.3 to +16.0 points, includes zero): " +
                   UNDETECTED + ". " + AUTHORITATIVE)
        lines = record.splitlines()
        self.assertEqual(lines[:2], ["# Migration decision record", ""])
        self.assertTrue(lines[2].startswith("Decision summary: " + summary), lines[2])
        self.assertIn("Accuracy among on-list answers only: old 100.0%, new 83.3% (difference -16.7 points; "
                      "95% interval", record)
        page = (self.root / "format.html").read_text()
        self.assertIn('id="decision-summary"', page)
        decision = json.loads((self.root / "format.decision.json").read_text())
        self.assertTrue(decision["decision_summary"].startswith(summary))
        self.assert_browser_markdown_matches(self.root / "format.html")

    def test_summary_says_format_not_capability_when_on_list_accuracy_is_at_or_above_old(self):
        # New draw 0 entirely off-list, draw 1 all correct: on-list accuracy 100% against old 100%.
        directory = self.off_list_run(list(GOLD), name="held")
        summary = decision_summary(analyze(*load_run(directory)))
        self.assertIn("Among on-list answers, accuracy is 100.0% (old 100.0%): " + FORMAT + ".", summary)
        self.assertNotIn("not ruled out", summary)

    def test_summary_says_both_changed_when_on_list_accuracy_drops_beyond_the_interval(self):
        directory = self.off_list_run(GOLD[:3] + ["todo_list"] * 3, name="both")
        analysis = analyze(*load_run(directory))
        on_list = analysis["gold"]["on_list"]
        self.assertEqual((on_list["new"]["correct"], on_list["new"]["answers"]), (3, 6))
        self.assertLess(on_list["ci95"][1], 0)
        summary = decision_summary(analysis)
        self.assertIn("Among on-list answers, accuracy is 50.0% (old 100.0%), lower beyond the 95% interval: both "
                      "the output format and task accuracy on the answers that follow the format changed.", summary)
        self.assertNotIn("not a loss of task accuracy", summary)
        self.assertIn(AUTHORITATIVE, summary)

    def test_summary_without_hazards_or_change_says_no_regression_detected(self):
        directory = self.write_run([[clean(GOLD)], [clean(GOLD)]], name="same")
        analysis = analyze(*load_run(directory))
        summary = decision_summary(analysis)
        self.assertTrue(summary.startswith("No regression detected across the tested inputs and draws. "), summary)
        self.assertIn("all answers parse and use allowed labels".capitalize(), summary)
        self.assertNotIn("Among on-list", summary)
        self.assertNotRegex(summary.lower(), r"\bsafe")

    def test_summary_with_missing_answers_and_without_gold(self):
        directory = self.demo_like(gold=True)
        summary = decision_summary(analyze(*load_run(directory)))
        self.assertIn("6 of 12 answers parse; the 6 missing or unparseable are in 1 of 2 calls.", summary)
        self.assertIn("Among on-list answers, accuracy is 83.3% (old 100.0%; the 95% interval of the difference, "
                      "-49.3 to +16.0 points, includes zero): " + UNDETECTED, summary)
        self.assertNotIn(FORMAT, summary)
        self.assertTrue(summary.endswith(" 1 input queued for review (1 stable regression)."), summary)
        directory = self.demo_like(gold=False, name="nogold")
        summary = decision_summary(analyze(*load_run(directory)))
        self.assertTrue(summary.startswith("No gold labels, so accuracy is not measured; 0 of 6 inputs without gold "
                                           "changed beyond run-to-run noise."), summary)
        self.assertNotIn("No regression detected", summary)
        self.assertNotIn("accuracy is", summary.split("; ", 1)[1])

    def demo_like(self, gold, name="missing"):
        missing = pp.attempt("not json", [None] * 6, "unparseable", objects=0)
        first = list(GOLD[:5]) + ["todo_list"]
        return self.write_run([[missing, missing, missing], [clean(first)]], name=name, gold=gold)

    def test_off_list_examples_only_with_show_text(self):
        directory = self.off_list_run(GOLD[:5] + ["todo_list"])
        build_report(directory, self.root / "hidden.html")
        for suffix in (".html", ".decision.md", ".decision.json", ".analysis.json"):
            text = (self.root / ("hidden" + suffix)).read_text()
            self.assertNotIn("TRAVEL_NOTIFICATION", text, suffix)
        self.assertIn("Rebuild with --show-text to list up to 3 per model", (self.root / "hidden.decision.md").read_text())
        build_report(directory, self.root / "shown.html", show_text=True)
        record = (self.root / "shown.decision.md").read_text()
        self.assertIn('Off-list examples, new (most frequent first): "DATE" (1 time) beside allowed "date"; "MPG" '
                      '(1 time) beside allowed "mpg"; "REMINDER" (1 time) beside allowed "reminder".', record)
        self.assert_browser_markdown_matches(self.root / "shown.html")

    def test_paired_interval_is_clustered_by_input(self):
        self.assertIsNone(paired_interval([(1, 1)], [(0, 1)]))
        self.assertIsNone(paired_interval([(1, 1), (1, 1)], [(0, 0), (0, 0)]))
        self.assertEqual(paired_interval([(2, 2)] * 4, [(2, 2)] * 4), [0.0, 0.0])
        # Doubling every input's draws leaves the interval unchanged: draws of one input are one cluster.
        once = paired_interval([(1, 1), (1, 1), (0, 1), (1, 1)], [(0, 1), (1, 1), (0, 1), (0, 1)])
        twice = paired_interval([(2, 2), (2, 2), (0, 2), (2, 2)], [(0, 2), (2, 2), (0, 2), (0, 2)])
        for a, b in zip(once, twice):
            self.assertAlmostEqual(a, b)


if __name__ == "__main__":
    unittest.main()
