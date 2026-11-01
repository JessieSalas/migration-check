"""The `sample` subcommand. Offline only: network access is blocked and no model is called."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from migration_check import __version__, providers, sample
from migration_check.__main__ import main, parser
from migration_check.protocol import read_inputs, read_labels, validate_gold
from migration_check.report import build_report

ROOT = Path(__file__).resolve().parents[1]
ARTEFACTS = ("report.html", "report.decision.json", "report.decision.md", "report.analysis.json")


class Stop(Exception):
    pass


class TtyStream(io.StringIO):
    def isatty(self):
        return True


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        blocker = patch.object(socket.socket, "connect", side_effect=AssertionError("Tests must not access network"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["sample", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_offline_sample_writes_every_artefact_and_is_labelled_simulated(self):
        out = self.root / "out"
        code, stdout, _ = self.cli("--offline", "--out", str(out))
        self.assertEqual(code, 0)
        for name in ARTEFACTS:
            self.assertGreater((out / name).stat().st_size, 0, name)
        manifest = json.loads((out / "runs" / "manifest.json").read_text())
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["models"], {"old": "mock:sample-old", "new": "mock:sample-new"})
        self.assertEqual((manifest["draws"], manifest["batch"], manifest["workers"], manifest["n_items"]), (3, 20, 8, 100))
        analysis = json.loads((out / "report.analysis.json").read_text())
        self.assertTrue(analysis["simulated"])
        self.assertTrue(analysis["warnings"][0].startswith("SIMULATED:"))
        self.assertTrue(json.loads((out / "report.decision.json").read_text())["simulated"])
        self.assertIn("SIMULATED: mock providers answered", (out / "report.decision.md").read_text())
        html = (out / "report.html").read_text()
        self.assertIn('"simulated": true', html)
        self.assertIn("SIMULATED REPORT", html)
        # A non-trivial report: format failures on both arms and queued changes.
        hazards = analysis["hazard_summary"]
        self.assertGreater(hazards["new"]["parse_failures"], 0)
        self.assertGreater(hazards["old"]["patterns"].get("case_change", 0), 0)
        self.assertTrue(analysis["review_queue"])
        self.assertNotEqual(analysis["gold"]["old_accuracy"], analysis["gold"]["new_accuracy"])
        # stdout: simulated label, elapsed seconds, report URL, decision paths and where to look first.
        self.assertTrue(stdout.startswith("SIMULATED:"))
        self.assertIn("SIMULATED REPORT", stdout)
        self.assertRegex(stdout, r"Done in [0-9]+\.[0-9] s\.")
        self.assertIn((out / "report.html").resolve().as_uri(), stdout)
        self.assertIn(str((out / "report.decision.json").resolve()), stdout)
        self.assertIn(str((out / "report.decision.md").resolve()), stdout)
        self.assertIn("Look first at 01 Format hazards", stdout)
        self.assertIn("should use 10 draws", stdout)

    def test_missing_claude_cli_fails_before_any_work(self):
        out = self.root / "nothing-here"
        with patch("shutil.which", return_value=None), \
                patch.object(sample, "run", side_effect=AssertionError("run must not start")):
            code, stdout, stderr = self.cli("--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("claude CLI not found", stderr)
        self.assertIn("migration-check sample --offline", stderr)
        self.assertIn("anthropic:MODEL or openai:MODEL", stderr)
        self.assertIn("not yet live-tested", stderr)
        self.assertIn("npm install -g @anthropic-ai/claude-code", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(out.exists())
        # Only an arm that uses claude-cli needs the CLI.
        with patch("shutil.which", return_value=None), patch.object(sample, "run", side_effect=Stop):
            with self.assertRaises(Stop), redirect_stdout(io.StringIO()):
                sample.run_sample(parser().parse_args(["sample", "--old", "mock:old", "--new", "mock:new",
                                                       "--out", str(self.root / "mocked")]))

    def test_defaults_are_the_sonnet_pair_through_the_claude_cli(self):
        seen = {}

        def capture(args):
            seen.update(vars(args))
            raise Stop

        with patch("shutil.which", return_value="/usr/local/bin/claude"), patch.object(sample, "run", side_effect=capture):
            with self.assertRaises(Stop), redirect_stdout(io.StringIO()):
                sample.run_sample(parser().parse_args(["sample", "--out", str(self.root / "live")]))
        self.assertEqual((seen["old"], seen["new"]),
                         ("claude-cli:claude-sonnet-4-5-20250929", "claude-cli:claude-sonnet-5-5"))
        self.assertEqual((seen["draws"], seen["batch"], seen["workers"]), (3, 20, 8))
        self.assertEqual(Path(seen["out"]), self.root / "live" / "runs")
        self.assertIsNone(parser().parse_args(["sample"]).out)
        # The live default runs the public Banking77 subset, not the synthetic set the offline mocks were written for.
        self.assertEqual(Path(seen["inputs"]).parent.name, "banking77")
        self.assertEqual(len(read_labels(seen["labels"])), 77)

    def test_offline_and_live_default_to_separate_directories(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(self.cli("--offline", "--draws", "1")[0], 0)
        self.assertTrue((self.root / "migration-check-sample-offline" / "report.html").is_file())
        seen = {}

        def capture(args):
            seen.update(vars(args))
            raise Stop

        with patch("shutil.which", return_value="/usr/local/bin/claude"), patch.object(sample, "run", side_effect=capture):
            with self.assertRaises(Stop), redirect_stdout(io.StringIO()):
                sample.run_sample(parser().parse_args(["sample"]))
        # The live run lands in its own top-level folder, never inside the simulated one.
        self.assertEqual(Path(seen["out"]), Path("migration-check-sample") / "runs")

    def test_failed_start_leaves_nothing_behind_and_closes_the_progress_line(self):
        out = self.root / "failed"
        args = parser().parse_args(["sample", "--old", "mock:fail", "--new", "mock:fail", "--out", str(out)])
        err = TtyStream()
        with self.assertRaisesRegex(ValueError, "Stopped early.*Nothing was kept; rerun `migration-check sample`"):
            sample.run_sample(args, stdout=io.StringIO(), stderr=err)
        self.assertFalse(out.exists())
        self.assertIn("calls", err.getvalue())
        self.assertTrue(err.getvalue().endswith("\n"))
        # A directory the user made beforehand is kept, empty, for the next attempt.
        out.mkdir()
        with self.assertRaisesRegex(ValueError, "Stopped early"):
            sample.run_sample(args, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(list(out.iterdir()), [])

    def test_sample_report_shows_ticket_text_and_gold_for_each_queued_item(self):
        out = self.root / "text"
        code, stdout, _ = self.cli("--offline", "--out", str(out))
        self.assertEqual(code, 0)
        record = json.loads((out / "report.decision.json").read_text())
        tickets = {row["id"]: row for row in read_inputs(sample.sample_resource("tickets.jsonl"))}
        self.assertTrue(record["items"])
        markdown = (out / "report.decision.md").read_text()
        for item in record["items"]:
            self.assertEqual(item["input"], tickets[item["id"]]["input"])
            self.assertEqual(item["gold"], tickets[item["id"]]["gold"])
            self.assertIn("Gold: " + item["gold"], markdown)
        self.assertIn("Input: ", markdown)
        self.assertRegex(markdown, r"Stable (regression|improvement): old correct in [0-9]+ of 3 draws")
        self.assertRegex(markdown, r"Accuracy on gold: old [0-9.]+%, new [0-9.]+% \(D\* [-+][0-9.]+ percentage points")
        self.assertNotIn('"pending human review"', markdown)
        self.assertIn("each card shows the ticket text, its gold label", stdout)
        self.assertIn("report --runs " + str(out / "runs") + " --out " + str(out / "report.html") + " --show-text", stdout)

    def test_program_name_matches_how_the_tool_was_started(self):
        with patch.object(sys, "argv", ["/somewhere/site-packages/migration_check/__main__.py"]):
            self.assertEqual(providers.program_name(), "python3 -m migration_check")
            self.assertIn("python3 -m migration_check sample --offline", providers.claude_missing())
        with patch.object(sys, "argv", ["/somewhere/bin/migration-check"]):
            self.assertEqual(providers.program_name(), "migration-check")

    def test_pyproject_version_matches_package_version(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(re.search(r'(?m)^version = "([^"]+)"$', pyproject)[1], __version__)

    def test_bundled_sample_reads_and_validates(self):
        tickets, labels = sample.sample_resource("tickets.jsonl"), sample.sample_resource("labels.txt")
        self.assertTrue(tickets.is_file() and labels.is_file())
        rows = read_inputs(tickets)
        label_list = read_labels(labels)
        self.assertEqual([row["id"] for row in rows], [f"sample-{i:03d}" for i in range(1, 101)])
        self.assertEqual(len(label_list), 10)
        validate_gold(rows, label_list)
        self.assertTrue(all("gold" in row for row in rows))

    def test_banking77_sample_is_ten_intents_of_ten_against_all_77_labels_with_its_notice(self):
        tickets = sample.sample_resource("tickets.jsonl", "banking77")
        labels = sample.sample_resource("labels.txt", "banking77")
        rows, label_list = read_inputs(tickets), read_labels(labels)
        self.assertEqual([row["id"] for row in rows], [f"b77-{i:03d}" for i in range(1, 101)])
        self.assertEqual(len(label_list), 77)
        validate_gold(rows, label_list)
        counts = {}
        for row in rows:
            counts[row["gold"]] = counts.get(row["gold"], 0) + 1
            self.assertEqual(row["slice"], row["gold"])
        self.assertEqual(sorted(counts.values()), [10] * 10)
        self.assertIn("transfer_into_account", counts)
        notice = sample.sample_resource("NOTICE", "banking77").read_text(encoding="utf-8")
        self.assertIn("CC BY 4.0", notice)
        self.assertIn("Casanueva", notice)

    def test_draws_override_changes_the_run(self):
        out = self.root / "two"
        code, stdout, _ = self.cli("--offline", "--draws", "2", "--out", str(out))
        self.assertEqual(code, 0)
        manifest = json.loads((out / "runs" / "manifest.json").read_text())
        self.assertEqual(manifest["draws"], 2)
        calls = [json.loads(line) for line in (out / "runs" / "calls.jsonl").read_text().splitlines()]
        self.assertEqual({(c["arm"], c["draw"], c["batch"]) for c in calls},
                         {(arm, draw, batch) for arm in ("old", "new") for draw in range(2) for batch in range(5)})
        self.assertIn("Draws: 2 per model", stdout)
        self.assertEqual(json.loads((out / "report.decision.json").read_text())["draws"], 2)
        self.assertEqual(self.cli("--offline", "--draws", "0", "--out", str(self.root / "zero"))[0], 2)
        self.assertFalse((self.root / "zero").exists())

    def test_rerun_never_touches_an_earlier_run_or_reviewer_edits(self):
        out = self.root / "again"
        self.assertEqual(self.cli("--offline", "--out", str(out))[0], 0)
        decision = out / "report.decision.json"
        record = json.loads(decision.read_text())
        record["reviewer"], record["decision"] = "Synthetic Reviewer", "hold"
        decision.write_text(json.dumps(record))
        before = {path: path.read_bytes() for path in out.rglob("*") if path.is_file()}
        code, stdout, _ = self.cli("--offline", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        subdirs = [path for path in out.iterdir() if path.is_dir() and path.name.startswith("run-")]
        self.assertEqual(len(subdirs), 1)
        for name in ARTEFACTS:
            self.assertTrue((subdirs[0] / name).is_file(), name)
        self.assertIn(subdirs[0].resolve().as_uri(), stdout)

    def test_offline_refuses_explicit_models(self):
        out = self.root / "conflict"
        code, _, stderr = self.cli("--offline", "--old", "mock:old", "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("--offline uses the mock providers", stderr)
        self.assertFalse(out.exists())

    def test_real_provider_reports_are_not_labelled_simulated(self):
        out = self.root / "real"
        self.assertEqual(self.cli("--offline", "--draws", "2", "--out", str(out))[0], 0)
        manifest_path = out / "runs" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["models"] = {"old": "openai:old-model", "new": "openai:new-model"}
        manifest_path.write_text(json.dumps(manifest))
        report = self.root / "real.html"
        build_report(out / "runs", report)
        analysis = json.loads(report.with_suffix(".analysis.json").read_text())
        self.assertNotIn("simulated", analysis)
        self.assertFalse(any(w.startswith("SIMULATED") for w in analysis["warnings"]))
        self.assertNotIn("SIMULATED", report.with_suffix(".decision.md").read_text())
        self.assertNotIn("simulated", json.loads(report.with_suffix(".decision.json").read_text()))


if __name__ == "__main__":
    unittest.main()
