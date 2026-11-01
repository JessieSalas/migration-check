"""Durable local run capture, with every failed attempt retained."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import secrets
import sys
import threading
import time
import uuid

from . import __version__, providers
from .archive import digest, inputs_digest, load_archive, replay_calls, text_digest
from .protocol import (SYSTEM, build_request, parse_answer, pattern, read_inputs, read_labels,
                       recover_keyed, validate_gold)
from .tools import (REPLY_FAILURES, STOP_HAZARDS, TOOL_HAZARDS, TOOL_SYSTEM,
                    parse_tool_reply, read_tools, stop_hazards)


CAPTURE_FILES = ("manifest.json", "inputs.jsonl", "calls.jsonl", "privacy-salt")


class EarlyStop(ValueError):
    """The first batch failed at the provider for at least one arm; nothing was answered."""

    def __init__(self, message, out):
        super().__init__(message)
        self.out = out


def answered_nothing(out):
    """True for a capture that stopped before completing and holds no parsed answer.

    That is what a failed start leaves behind (no CLI login, a wrong model ID or key),
    so the same command can be rerun into the same directory after the fix.
    """
    try:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("complete") is not False:
            return False
        calls = (out / "calls.jsonl").read_text(encoding="utf-8") if (out / "calls.jsonl").exists() else ""
        for line in calls.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("archived") or any(
                    answer is not None for attempt in record.get("attempts", [])
                    for answer in attempt.get("parsed_answers", [])):
                return False
    except (OSError, ValueError, AttributeError, TypeError):
        return False
    return True


def set_aside_failed_start(out):
    """Move a failed start's capture files into a subdirectory; never delete them."""
    stamp = datetime.now(timezone.utc).strftime("failed-start-%Y%m%dT%H%M%SZ")
    for suffix in [""] + [f"-{n}" for n in range(2, 1000)]:
        aside = out / (stamp + suffix)
        try:
            aside.mkdir()
        except FileExistsError:
            continue
        for name in CAPTURE_FILES:
            if (out / name).exists():
                (out / name).rename(aside / name)
        return aside
    raise ValueError(f"Could not set aside the failed start in {out}; choose a new --out directory")


def run(args):
    if min(args.draws, args.batch, args.workers) < 1 or args.retries < 0 or args.timeout <= 0:
        raise ValueError("Draws, batch, workers and timeout must be positive; retries cannot be negative")
    rows, labels = read_inputs(args.inputs), read_labels(args.labels)
    answer_mode = getattr(args, "answer", "text")
    tools_path = getattr(args, "tools", None)
    tool_mode = answer_mode == "tool"
    hazard_keys = TOOL_HAZARDS if tool_mode else STOP_HAZARDS
    if tool_mode and (args.batch != 1 or not tools_path or args.labels):
        raise ValueError("--answer tool requires --tools, --batch 1 (tool calls are per conversation), and no --labels")
    if tools_path and not tool_mode:
        raise ValueError("--tools requires --answer tool")
    tools = read_tools(tools_path) if tools_path else []
    if tool_mode:
        labels = [t["function"]["name"] for t in tools] + ["none"]
    validate_gold(rows, labels, "tool names plus 'none'" if tool_mode else "labels.txt")
    system = Path(args.system).read_text(encoding="utf-8-sig") if args.system else TOOL_SYSTEM if tool_mode else SYSTEM
    template = Path(args.template).read_text(encoding="utf-8-sig") if args.template else None
    if template is not None and "{input}" not in template:
        raise ValueError("User template must contain {input}")
    archived, archive_salt, old_model = None, None, args.old
    requested_draws, prompt_changes = args.draws, {}
    if args.old.startswith("archive:"):
        archive_path = args.old.partition(":")[2]
        archive_path, separator, expected_head = archive_path.rpartition("@") if "@" in archive_path else (archive_path, "", None)
        frozen, entries, archive_salt = load_archive(archive_path, expected_head if separator else None)
        for name, value in (("system", system), ("template", template)):
            actual = text_digest(value)
            if actual != frozen[name + "_sha256"]:
                prompt_changes[name] = {"archive_sha256": frozen[name + "_sha256"], "run_sha256": actual}
        if prompt_changes and not getattr(args, "allow_prompt_change", False):
            raise ValueError("Prompt differs from archive: " + ", ".join(prompt_changes) +
                             "; use matching prompts or explicit --allow-prompt-change")
        if inputs_digest(rows) != frozen["inputs_sha256"]:
            raise ValueError("Inputs sha256 differs from the archive")
        if (answer_mode != frozen["answer"] or labels != frozen["labels"] or
                (digest(tools) if tools else None) != frozen["tools_sha256"]):
            raise ValueError("Answer mode, labels and tools must match the archive")
        if args.batch != frozen["batch"]:
            raise ValueError(f"Archive requires --batch {frozen['batch']}")
        args.draws = frozen["draws"]
        old_model = frozen["model"]
        archived = replay_calls(frozen, entries, rows, archive_salt)
    for spec in ((args.old, args.new) if archived is None else (args.new,)):
        provider, model = providers.split_model(spec)
        if tool_mode and provider == "claude-cli":
            raise ValueError("claude-cli does not support tool mode; use anthropic, openai, openai-compatible or mock")
        if tool_mode and provider == "mock" and model not in ("correct", "wrong_tool", "none", "bad_args", "flaky"):
            raise ValueError("Tool mock modes: correct, wrong_tool, none, bad_args, flaky")
        providers.preflight(spec)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if any((out / name).exists() for name in CAPTURE_FILES):
        if not answered_nothing(out):
            raise ValueError("Run directory already contains audit data; choose a new --out directory")
        aside = set_aside_failed_start(out)
        print(f"An earlier start in {out} failed before any answer; its files were moved to {aside}.",
              file=sys.stderr)
    manifest = {"schema_version": 1, "version": __version__, "audit_id": uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "models": {"old": old_model, "new": args.new}, "draws": args.draws, "batch": args.batch,
                "workers": args.workers, "retries": args.retries, "timeout": args.timeout, "labels": labels,
                "n_items": len(rows), "system": system, "template": template, "complete": False,
                "answer": answer_mode, "tools": tools}
    if archived is not None:
        manifest["archive_replay"] = {"path": archive_path, "head_hash": frozen["head_hash"], "manifest": frozen,
                                      "expected_head": expected_head, "prompt_changes": prompt_changes,
                                      "requested_draws": requested_draws}
        manifest["requested_draws"] = requested_draws
        if requested_draws != args.draws:
            manifest["draws_notice"] = (f"Archive has R={args.draws}; --draws {requested_draws} ignored; "
                                        f"the new arm runs {args.draws} draws")
            print(manifest["draws_notice"])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out / "inputs.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    with os.fdopen(os.open(out / "privacy-salt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(archive_salt if archived is not None else secrets.token_bytes(32))
    batches = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
    stopping = threading.Event()
    batched_text = not tool_mode and args.batch > 1

    def one(job):
        arm, draw, batch = job
        if arm == "old" and archived is not None:
            return archived[draw, batch]
        spec, items = manifest["models"][arm], batches[batch]
        request_system, messages = build_request(items, [] if tool_mode else labels, system, template, args.batch > 1)
        attempts, predictions = [], [None] * len(items)
        for number in range(args.retries + 1):
            started = time.monotonic()
            attempt = {"attempt": number, "timestamp": datetime.now(timezone.utc).isoformat(),
                       "raw_text": "", "served_models": [], "usage": {}, "error": None,
                       "stop_reason": None, "finish_reason": None, "message_refusal": None,
                       **dict.fromkeys(hazard_keys, False)}
            if tool_mode:
                attempt.update(n_tool_calls=0, args_valid=None, tool_calls=[])
            if batched_text:
                attempt["json_objects"] = 0
            try:
                response = providers.call(spec, request_system, messages,
                                          {"labels": labels, "draw": draw, "batch": batch, "attempt": number,
                                           "size": len(items), "batched": args.batch > 1,
                                           "answer": answer_mode, "tools": tools, "arm": arm,
                                           "input": (items[0]["input"] if "input" in items[0] else
                                                     "\n".join(m["content"] for m in items[0]["messages"])),
                                           "item_id": items[0]["id"],
                                           # Mocks only; gold and slice never enter the context.
                                           "item_ids": [row["id"] for row in items],
                                           "inputs": [row["input"] if "input" in row else
                                                      "\n".join(m["content"] for m in row["messages"])
                                                      for row in items]}, timeout=args.timeout)
                attempt.update(raw_text=response["text"], served_models=response["served_models"],
                               usage=response.get("usage", {}), error=response.get("error"),
                               **{key: response.get(key) for key in ("stop_reason", "finish_reason")},
                               message_refusal=response.get("refusal"),
                               **stop_hazards(response))
                if batched_text:
                    # Descriptive only: a reply holding a second, corrected JSON object
                    # still parses as a failure below, and predictions never use this.
                    n_objects, recovered = recover_keyed(response["text"] or "", len(items))
                    attempt["json_objects"] = n_objects
                    if n_objects >= 2:
                        attempt.update(self_correction=True, recovered_answers=recovered)
                if tool_mode and not attempt["error"]:
                    tool_result = parse_tool_reply(response, tools)
                    predictions = [tool_result["answer"]]
                    attempt.update({key: value for key, value in tool_result.items() if key != "answer"})
                else:
                    predictions = (parse_answer(response["text"], len(items), args.batch > 1)
                                   if not attempt["error"] and not any(attempt[key] for key in STOP_HAZARDS)
                                   else [None] * len(items))
                if not attempt["error"]:
                    attempt["error"] = next((key for key in STOP_HAZARDS if attempt[key]), None)
                if tool_mode and predictions == [None] and attempt["unknown_tool"] and not attempt["error"]:
                    attempt["error"] = "unknown_tool"
                if not any(p is not None for p in predictions) and not attempt["error"]:
                    attempt["error"] = "unparseable"
            except Exception as exc:
                # Exception messages from SDKs/OS/network stacks can echo prompts or keys.
                attempt["error"] = str(exc) if isinstance(exc, providers.ProviderError) else type(exc).__name__
                predictions = [None] * len(items)
            if tool_mode and attempt["error"]:
                attempt["error"] = providers.safe_tool_error(attempt["error"])
            attempt["latency_seconds"] = round(time.monotonic() - started, 6)
            attempt["parsed_answers"] = predictions
            attempt["parse_failures"] = sum(p is None for p in predictions)
            attempts.append(attempt)
            # A partial keyed answer ends the call. Semantic failures are final
            # outcomes too, so retries cannot hide refusals or incomplete replies.
            if (any(p is not None for p in predictions) or stopping.is_set() or
                    attempt["error"] in STOP_HAZARDS + ("unknown_tool",)):
                break
        tool_checks = {key: attempts[-1][key] for key in
                       (("args_valid", "n_tool_calls") + hazard_keys if tool_mode else hazard_keys)}
        return {"arm": arm, "requested_model": spec, "draw": draw, "batch": batch, **tool_checks,
                "timestamp": attempts[0]["timestamp"],
                "item_ids": [row["id"] for row in items], "attempts": attempts, "retries": len(attempts) - 1,
                "latency_seconds": round(sum(a["latency_seconds"] for a in attempts), 6),
                "error": attempts[-1]["error"],
                "predictions": [{"id": row["id"], "answer": answer, "pattern": pattern(answer, labels)}
                                for row, answer in zip(items, predictions)]}

    count, archived_count = 0, 0
    progress, total = getattr(args, "progress", None), 2 * args.draws * len(batches)
    lock = threading.Lock()
    with (out / "calls.jsonl").open("x", encoding="utf-8") as stream:
        executor = ThreadPoolExecutor(args.workers)

        def keep(job):
            nonlocal count, archived_count
            record = one(job)
            with lock:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                count += 1
                archived_count += int(bool(record.get("archived")))
                if progress:
                    progress(count, total)
            return record

        def submit(job):
            return executor.submit(keep, job)

        try:
            # Probe the first batch of each arm before scheduling the full audit.
            probes = [submit((arm, 0, 0)) for arm in ("old", "new")]
            failed = [f.result() for f in probes]
            failed = [c for c in failed if not c.get("archived") and
                      all(a["error"] and a["error"] not in REPLY_FAILURES for a in c["attempts"])]
            if failed:
                details = "; ".join(c["arm"] + ": " + str(c["error"]) for c in failed)
                next_step = ("rerunning the same command reuses that directory and moves them aside."
                             if answered_nothing(out) else
                             f"before rerunning, remove it (rm -r {out}) or choose a new --out directory.")
                raise EarlyStop("Stopped early: every provider attempt in the first batch failed for " + details +
                                ". Check credentials, model ID and URL; for claude-cli, run `claude` once and "
                                f"log in with /login. The attempts are kept in {out}; " + next_step, out)
            futures = [submit((arm, draw, batch)) for draw in range(args.draws)
                       for batch in range(len(batches)) for arm in ("old", "new") if (draw, batch) != (0, 0)]
            for future in futures:
                future.result()
        finally:
            stopping.set()
            # Workers persist every completed/in-flight result, even on Ctrl-C.
            executor.shutdown(wait=True, cancel_futures=True)
    manifest["complete"] = True
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"calls": count, "live_calls": count - archived_count, "archived_calls": archived_count,
            "calls_per_model_without_retries": args.draws * len(batches), "directory": str(out)}
