"""Freeze one captured arm and replay it without contacting its provider."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

from . import __version__, providers
from .privacy import answer_key
from .protocol import normalize, pattern, recover_keyed
from .tools import STOP_HAZARDS, TOOL_HAZARDS


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def inputs_digest(rows):
    # Preserve item order and all metadata, ignoring JSON formatting and BOM/CRLF.
    return hashlib.sha256(b"".join(canonical(row) + b"\n" for row in rows)).hexdigest()


def text_digest(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def chain_hash(previous, line):
    return hashlib.sha256(bytes.fromhex(previous) + line).hexdigest()


def manifest_seed(manifest):
    """Pin every semantic manifest field in the answer chain's initial hash."""
    return digest({key: value for key, value in manifest.items()
                   if key not in ("head_hash", "manifest_sha256")})


def create_archive(runs, arm, output, include_raw=False):
    from .analysis import load_run

    source, rows, calls = load_run(runs)
    selected = {(c["draw"], c["batch"]): c for c in calls if c["arm"] == arm}
    batch_count = (len(rows) + source["batch"] - 1) // source["batch"]
    if len(selected) != source["draws"] * batch_count:
        raise ValueError("Cannot archive an incomplete arm: missing calls")
    out = Path(output)
    # Never overwrite an archive or mix its private key with unrelated files.
    if out.exists() and any(out.iterdir()):
        raise ValueError("Archive directory must be empty; choose a new --out directory")
    inputs_sha256 = inputs_digest(rows)
    salt = secrets.token_bytes(32)
    labels = source["labels"]
    replay_salt = bytes.fromhex(source["_answer_salt"]) if source.get("_answer_salt") else None
    replayed = any(c.get("archived") for c in selected.values())
    provenance = source["archive_replay"]["manifest"] if replayed else None
    hazards = TOOL_HAZARDS if source.get("answer") == "tool" else STOP_HAZARDS
    # Retain lexical tie ordering without retaining any free text.
    values = sorted({normalize(p["answer"], labels) for c in selected.values() for p in c["predictions"]
                     if p["answer"] is not None})
    ranks = {value: i for i, value in enumerate(values)}
    lines = []
    for draw in range(source["draws"]):
        for batch in range(batch_count):
            call = selected[draw, batch]
            attempts = []
            for attempt in call["attempts"]:
                attempts.append({"served_models": providers.model_ids(attempt.get("served_models")),
                                 "error": (providers.safe_tool_error(attempt["error"]) if source.get("answer") == "tool"
                                           else attempt["error"]) if attempt.get("error") else None,
                                 "parse_failures": attempt["parse_failures"],
                                 "latency_seconds": attempt.get("latency_seconds", 0),
                                 "timestamp": attempt.get("timestamp", call.get("timestamp", source["created_at"]))})
                if "json_objects" in attempt:
                    # Counts and pseudonymised answers only, never reply text.
                    attempts[-1].update(json_objects=attempt["json_objects"],
                                        self_correction=bool(attempt.get("self_correction")))
                    if attempt.get("recovered_answers") is not None:
                        attempts[-1]["recovered_answers"] = [
                            value if call.get("archived") or value is None else
                            answer_key(normalize(value, labels), labels, salt) for value in attempt["recovered_answers"]]
            for pred in call["predictions"]:
                value = normalize(pred["answer"], labels)
                # Re-archiving a replay retains its existing pseudonyms and key.
                encoded = bool(call.get("archived"))
                entry = {"id": pred["id"], "draw": draw, "batch": batch,
                         "answer": pred["answer"] if encoded else answer_key(value, labels, salt),
                         "answer_rank": pred.get("answer_rank", ranks.get(value)),
                         "pattern": pred.get("pattern") if encoded else pattern(pred["answer"], labels),
                         "provider": providers.split_model(source["models"][arm])[0],
                         "served_models": attempts[-1]["served_models"],
                         "timestamp": call.get("timestamp", source["created_at"]),
                         "timestamp_source": call.get("timestamp_source", "call" if call.get("timestamp") else "run"),
                         "latency_seconds": call["latency_seconds"],
                         "hazards": {key: bool(call.get(key)) for key in hazards},
                         "attempts": attempts}
                if include_raw:
                    entry["raw_text"] = [a.get("raw_text", "") for a in call["attempts"]]
                line = canonical(entry) + b"\n"
                lines.append(line)
    # A replayed arm already has hashed answers. Keep the key for future comparisons.
    if replayed:
        salt = replay_salt
    manifest = {"schema_version": 2, "kind": "migration-check-archive", "version": __version__,
                "source_version": source["version"], "source_arm": arm,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_created_at": source["created_at"], "source_audit_id": source.get("audit_id"),
                "served_models": sorted({model for c in selected.values() for a in c["attempts"]
                                         for model in providers.model_ids(a.get("served_models"))}),
                "model": source["models"][arm], "draws": source["draws"], "batch": source["batch"],
                "n_items": len(rows), "answer": source.get("answer", "text"), "labels": labels,
                "inputs_sha256": inputs_sha256, "labels_sha256": digest(labels) if labels else None,
                "tools_sha256": digest(source["tools"]) if source.get("tools") else None,
                "template_sha256": text_digest(source.get("template")), "system_sha256": text_digest(source["system"]),
                "include_raw": include_raw, "answer_count": len(lines),
                "salt_sha256": hashlib.sha256(salt).hexdigest()}
    if provenance:
        for field in ("source_version", "source_created_at", "source_audit_id", "template_sha256", "system_sha256"):
            manifest[field] = provenance[field]
    head = manifest_seed(manifest)
    for line in lines:
        head = chain_hash(head, line)
    manifest["head_hash"] = head
    manifest["manifest_sha256"] = digest(manifest)
    manifest_bytes = canonical(manifest) + b"\n"
    # All validation/serialization precedes file creation. Publish only complete archives.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".migration-archive-", dir=out.parent) as temporary:
        staging = Path(temporary) / "archive"
        staging.mkdir()
        with os.fdopen(os.open(staging / "privacy-salt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(salt)
        (staging / "answers.jsonl").write_bytes(b"".join(lines))
        (staging / "manifest.json").write_bytes(manifest_bytes)
        load_archive(staging)
        staging.rename(out)
    return {"directory": str(out), "answers": len(lines), "head_hash": head,
            "notice": "Record this head_hash outside the archive to detect deliberate edits."}


def load_archive(directory, expect_head=None):
    directory = Path(directory)
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if (manifest.get("kind") != "migration-check-archive" or manifest.get("schema_version") != 2):
        raise ValueError("Unsupported archive schema; recreate schema 1 archives from their source captures")
    if manifest_bytes != canonical(manifest) + b"\n":
        raise ValueError("Archive manifest encoding changed")
    checksum = manifest.pop("manifest_sha256", None)
    if checksum != digest(manifest):
        raise ValueError("Archive manifest checksum mismatch")
    manifest["manifest_sha256"] = checksum
    salt = (directory / "privacy-salt").read_bytes()
    if hashlib.sha256(salt).hexdigest() != manifest["salt_sha256"]:
        raise ValueError("Archive privacy salt checksum mismatch")
    head, entries = manifest_seed(manifest), []
    with (directory / "answers.jsonl").open("rb") as stream:
        for line in stream:
            head = chain_hash(head, line)
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("Invalid archive answers.jsonl") from exc
            entries.append(entry)
    if head != manifest["head_hash"] or len(entries) != manifest["answer_count"]:
        raise ValueError("Archive answer hash chain mismatch")
    if len(entries) != manifest["n_items"] * manifest["draws"]:
        raise ValueError("Archive answer dimensions disagree")
    if expect_head is not None and manifest["head_hash"] != expect_head:
        raise ValueError(f"Archive head differs from expected head: expected {expect_head}; found {manifest['head_hash']}")
    return manifest, entries, salt


def verify_archive(directory, expect_head=None):
    manifest, entries, _ = load_archive(directory, expect_head)
    return {"verified": True, "directory": str(directory), "answers": len(entries), "head_hash": manifest["head_hash"]}


def replay_calls(manifest, entries, rows, salt=None):
    """Reconstitute captured calls, preserving retry and format hazard counts.

    An archive made before the self-correction counts existed has none; when it
    holds raw replies and the archive salt is given, they are recomputed here.
    """
    grouped, seen = {}, set()
    for entry in entries:
        coordinate = entry["id"], entry["draw"]
        if coordinate in seen or not 0 <= entry["draw"] < manifest["draws"]:
            raise ValueError("Duplicate or invalid archive answer coordinates")
        seen.add(coordinate)
        grouped.setdefault((entry["draw"], entry["batch"]), []).append(entry)
    calls = {}
    for draw in range(manifest["draws"]):
        for batch, start in enumerate(range(0, len(rows), manifest["batch"])):
            group = grouped.get((draw, batch), [])
            ids = [row["id"] for row in rows[start:start + manifest["batch"]]]
            if [e["id"] for e in group] != ids:
                raise ValueError("Archive batch IDs do not match inputs")
            first = group[0]
            attempts = copy.deepcopy(first["attempts"])
            for attempt, raw in zip(attempts, first.get("raw_text", [""] * len(attempts))):
                attempt["raw_text"] = raw
                if ("json_objects" not in attempt and "raw_text" in first and salt is not None
                        and manifest["batch"] > 1 and manifest.get("answer", "text") != "tool"):
                    n_objects, recovered = recover_keyed(raw or "", len(ids))
                    attempt["json_objects"] = n_objects
                    if n_objects >= 2:
                        attempt.update(self_correction=True, recovered_answers=[
                            answer_key(normalize(value, manifest["labels"]), manifest["labels"], salt)
                            if value is not None else None for value in recovered])
            calls[draw, batch] = {"arm": "old", "archived": True, "requested_model": manifest["model"],
                                  "draw": draw, "batch": batch, "item_ids": ids, "attempts": attempts,
                                  "timestamp": first["timestamp"], "latency_seconds": first["latency_seconds"],
                                  "timestamp_source": first["timestamp_source"],
                                  "retries": len(attempts) - 1, "error": attempts[-1]["error"],
                                  **first["hazards"],
                                  "predictions": [{key: e[key] for key in ("id", "answer", "answer_rank", "pattern")}
                                                  for e in group]}
    if len(grouped) != len(calls):
        raise ValueError("Invalid archive batch coordinates")
    return calls
