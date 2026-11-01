"""`migration-check sample`: the bundled sample, from calls to decision record, in one command."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib import resources
import json
from pathlib import Path
import shutil
import sys
import time

from . import providers
from .report import build_report
from .runner import EarlyStop, run

DEFAULT_OLD = "claude-cli:claude-sonnet-4-5-20250929"
DEFAULT_NEW = "claude-cli:claude-sonnet-5-5"
OFFLINE_OLD, OFFLINE_NEW = "mock:sample-old", "mock:sample-new"
DEFAULT_DRAWS, AUDIT_DRAWS, BATCH, WORKERS = 3, 10, 20, 8
DEFAULT_OUT, OFFLINE_OUT = "migration-check-sample", "migration-check-sample-offline"
SAMPLE_FILES = ("tickets.jsonl", "labels.txt")
SIMULATED_NOTICE = ("SIMULATED: --offline uses mock providers (keyword matchers), so no model is called. "
                    "The report shows the layout and hazards; its numbers are not evidence about any model.")
TEXT_NOTE = ("The sample is {}, so its report shows each ticket's text. "
             "Reports on your own runs hide input text unless you pass --show-text.")
TEXT_SOURCE = {True: "synthetic", False: "public (Banking77, CC BY 4.0)"}
LIVE_SAMPLE = ("banking77", "public Banking77 support tickets (CC BY 4.0, see its NOTICE)")
OFFLINE_SAMPLE = ("", "synthetic support tickets")


def sample_resource(name, subdir=""):
    """The bundled sample as package data, so it works from an installed wheel.

    Live runs use the public Banking77 subset; --offline uses the synthetic set its mocks were written for.
    """
    base = resources.files("migration_check") / "sample"
    return (base / subdir / name) if subdir else (base / name)


def output_directory(out):
    """Use an empty or new directory; otherwise a fresh timestamped subdirectory.

    Existing files are never touched, so earlier reviewer edits survive. Returns the
    directory and whether this call created it.
    """
    out = Path(out)
    if out.exists() and not out.is_dir():
        raise ValueError(f"{out} exists and is not a directory; choose another --out")
    if not out.exists():
        out.mkdir(parents=True)
        return out, True
    if not any(out.iterdir()):
        return out, False
    stamp = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    for suffix in [""] + [f"-{n}" for n in range(2, 1000)]:
        candidate = out / (stamp + suffix)
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate, True
    raise ValueError(f"Could not create a fresh subdirectory in {out}; choose another --out")


def look_first(analysis):
    hazards, total = analysis["hazard_summary"], analysis["n_items"] * analysis["draws"]
    parts = [f"{arm} {hazards[arm]['parse_failures']} missing and {hazards[arm]['off_list']} off-list"
             for arm in ("old", "new")]
    queue = len(analysis["review_queue"])
    queue_text = (f"then the review queue ({queue} item{'s' if queue != 1 else ''} for you to judge; "
                  "each card shows the ticket text, its gold label and both models' answers).")
    corrected = [arm for arm in ("old", "new") if hazards[arm].get("self_correction")]
    if corrected:
        parts += [f"{arm} self-corrected in {hazards[arm]['self_correction']} "
                  f"call{'' if hazards[arm]['self_correction'] == 1 else 's'}" for arm in corrected]
    if corrected or any(hazards[arm]["parse_failures"] or hazards[arm]["off_list"] for arm in ("old", "new")):
        return ("Look first at 01 Format hazards (" + "; ".join(parts) + f", out of {total} answers per model), "
                + queue_text)
    return "No format hazards in this run. Look first at the draw strip, " + queue_text


def run_sample(args, stdout=None, stderr=None):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    started = time.monotonic()
    if args.offline:
        if args.old or args.new:
            raise ValueError("--offline uses the mock providers; drop --old/--new, or drop --offline")
        old, new = OFFLINE_OLD, OFFLINE_NEW
    else:
        old, new = args.old or DEFAULT_OLD, args.new or DEFAULT_NEW
    if args.draws < 1:
        raise ValueError("--draws must be positive")
    specs = (old, new)
    for spec in specs:
        providers.split_model(spec)
    # Fail before any file is written: no claude CLI, no key, or a bad base URL.
    for spec in specs:
        providers.preflight(spec)
    out_arg = args.out or (OFFLINE_OUT if args.offline else DEFAULT_OUT)
    with ExitStack() as stack:
        subdir, described = OFFLINE_SAMPLE if args.offline else LIVE_SAMPLE
        inputs, labels = (stack.enter_context(resources.as_file(sample_resource(name, subdir)))
                          for name in SAMPLE_FILES)
        if not Path(inputs).is_file() or not Path(labels).is_file():
            raise ValueError("Bundled sample data is missing from this install; reinstall migration-check")
        out, created = output_directory(out_arg)
        args.resolved_out = str(out)
        n_items = sum(1 for line in Path(inputs).read_text(encoding="utf-8-sig").splitlines() if line.strip())
        calls = 2 * args.draws * -(-n_items // BATCH)
        if args.offline:
            print(SIMULATED_NOTICE, file=stdout)
        print(f"Bundled sample: {n_items} {described}, {old} vs {new}.", file=stdout)
        print(f"Draws: {args.draws} per model, batch {BATCH}, {calls} calls before retries. "
              f"A real audit should use {AUDIT_DRAWS} draws (--draws {AUDIT_DRAWS}).", file=stdout)
        print(f"Writing to {out}", file=stdout, flush=True)
        live = stderr.isatty()
        shown = {"done": 0, "total": 0}

        def progress(done, total):
            shown.update(done=done, total=total)
            if live:
                print(f"\r  {done}/{total} calls", end="" if done < total else "\n", file=stderr, flush=True)

        run_args = argparse.Namespace(
            inputs=str(inputs), labels=str(labels), old=old, new=new, draws=args.draws, batch=BATCH,
            workers=WORKERS, answer="text", tools=None, system=None, template=None, allow_prompt_change=False,
            retries=2, timeout=240.0, out=str(out / "runs"), progress=progress)
        try:
            run(run_args)
        except EarlyStop as stop:
            # Nothing was answered: remove what this run wrote, so the next attempt
            # writes to the same place instead of leaving an orphan runs/ behind.
            shutil.rmtree(out / "runs", ignore_errors=True)
            if created:
                shutil.rmtree(out, ignore_errors=True)
            message = str(stop).split(" The attempts are kept in ")[0]
            raise ValueError(message + f" Nothing was kept; rerun `{providers.program_name()} sample` once that "
                                       "is fixed.") from None
        finally:
            # A progress line left open by an early stop or Ctrl-C must not run into the next message.
            if live and shown["done"] < shown["total"]:
                print("", file=stderr, flush=True)
    report = build_report(out / "runs", out / "report.html", show_text=True)
    analysis = json.loads((out / "report.analysis.json").read_text(encoding="utf-8"))
    elapsed = time.monotonic() - started
    print("", file=stdout)
    if analysis.get("simulated"):
        print("SIMULATED REPORT (mock providers; no model was called).", file=stdout)
    print(f"Done in {elapsed:.1f} s.", file=stdout)
    print("Report:          " + (out / "report.html").resolve().as_uri(), file=stdout)
    print("Decision record: " + str(Path(report["decision_json"]).resolve()), file=stdout)
    print("                 " + str(Path(report["decision_markdown"]).resolve()), file=stdout)
    print(look_first(analysis), file=stdout)
    print(TEXT_NOTE.format(TEXT_SOURCE[bool(args.offline)]), file=stdout)
    print("After you edit report.decision.json (or save the JSON downloaded from the page as report.decision.json), "
          "refresh the Markdown record with:", file=stdout)
    print(f"  {providers.program_name()} report --runs {out / 'runs'} --out {out / 'report.html'} --show-text",
          file=stdout)
    if args.draws < AUDIT_DRAWS:
        print(f"This used {args.draws} draws. For a decision, run your own inputs with --draws {AUDIT_DRAWS}.", file=stdout)
    return {"directory": str(out), "elapsed_seconds": round(elapsed, 3), "simulated": bool(analysis.get("simulated")),
            **report}
