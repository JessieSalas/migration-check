"""python -m migration_check"""

import argparse
import json
import sys

from . import __version__
from .archive import create_archive, verify_archive
from .report import build_report
from .runner import run
from .sample import AUDIT_DRAWS, DEFAULT_DRAWS, DEFAULT_NEW, DEFAULT_OLD, DEFAULT_OUT, OFFLINE_OUT, run_sample


def parser():
    ap = argparse.ArgumentParser(prog="migration-check", description="Local repeat-draw model migration audits. No telemetry.")
    ap.add_argument("--version", action="version", version=__version__)
    commands = ap.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="Run the bundled 100-ticket sample and write the report and decision record",
                                 description="Run the bundled sample end to end: calls, report.html and the decision record.")
    sample.add_argument("--old", help=f"provider:model (default: {DEFAULT_OLD})")
    sample.add_argument("--new", help=f"provider:model (default: {DEFAULT_NEW})")
    sample.add_argument("--draws", type=int, default=DEFAULT_DRAWS,
                        help=f"Draws per model (default: {DEFAULT_DRAWS}; a real audit should use {AUDIT_DRAWS})")
    sample.add_argument("--out",
                        help=f"Output directory (default: ./{DEFAULT_OUT}/, or ./{OFFLINE_OUT}/ with --offline); "
                             "if it is not empty, a new timestamped subdirectory is used")
    sample.add_argument("--offline", action="store_true",
                        help="Use mock providers: a simulated report in seconds, no claude CLI or key needed")
    runner = commands.add_parser("run", help="Call two models and keep all attempts locally")
    runner.add_argument("--inputs", required=True)
    runner.add_argument("--old", required=True, help="provider:model or archive:directory[@HEAD]")
    runner.add_argument("--new", required=True, help="provider:model")
    runner.add_argument("--draws", type=int, default=10, help="Draws per arm (default: 10); archive replay uses its stored count")
    runner.add_argument("--batch", type=int, default=1)
    runner.add_argument("--workers", type=int, default=4)
    runner.add_argument("--labels")
    runner.add_argument("--answer", choices=("text", "tool"), default="text", help="Parse text or the first tool choice")
    runner.add_argument("--tools", help="JSON list of function definitions (requires --answer tool)")
    runner.add_argument("--system")
    runner.add_argument("--template")
    runner.add_argument("--allow-prompt-change", action="store_true", help="Allow and report system/template differences from an archive")
    runner.add_argument("--retries", type=int, default=2, help="Extra attempts for fully failed calls (default: 2)")
    runner.add_argument("--timeout", type=float, default=240)
    runner.add_argument("--out", required=True)
    report = commands.add_parser("report", help="Write an offline HTML report and decision records")
    report.add_argument("--runs", required=True)
    report.add_argument("--out", required=True)
    report.add_argument("--show-text", action="store_true", help="Include local inputs and raw provider outputs in report artifacts")
    archive = commands.add_parser("archive", help="Freeze one captured model arm for future comparisons")
    archive.add_argument("--runs", required=True)
    archive.add_argument("--arm", choices=("old", "new"), required=True)
    archive.add_argument("--out", required=True)
    archive.add_argument("--include-raw", action="store_true", help="Include raw provider text (default: excluded)")
    verify = commands.add_parser("archive-verify", help="Verify an archive's answer hash chain")
    verify.add_argument("directory")
    verify.add_argument("--expect-head", help="Head hash recorded outside the archive")
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if args.command == "sample":
            run_sample(args)
            return 0
        if args.command == "run":
            result = run(args)
        elif args.command == "report":
            result = build_report(args.runs, args.out, args.show_text)
        elif args.command == "archive":
            result = create_archive(args.runs, args.arm, args.out, args.include_raw)
        else:
            result = verify_archive(args.directory, args.expect_head)
    except KeyboardInterrupt:
        output = args.directory if args.command == "archive-verify" else getattr(args, "resolved_out", args.out)
        print(f"migration-check: interrupted; partial run kept in {output}", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Validation errors are local. Provider exceptions are captured in calls.jsonl.
        print(f"migration-check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
