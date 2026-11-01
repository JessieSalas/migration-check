"""Offline report and reviewable decision exports, without raw replies by default."""

import copy
import hashlib
import html
import json
from pathlib import Path
import re
import secrets

from . import __version__
from .analysis import analyze, load_run
from .privacy import answer_key, read_salt
from .protocol import item_text
from .tools import TOOL_HAZARDS


SIMULATED_RECORD = "SIMULATED: mock providers answered; no model was called. This is not evidence about any model."
FILL_IN = ("How to fill this in: set decision, reviewer and notes, and each item's reviewer status, in the HTML "
           "report and click Download JSON. Save that file beside the report as REPORT.decision.json, where "
           "REPORT.html is the report (or edit REPORT.decision.json directly). Then rerun "
           "`migration-check report --runs RUNS_DIRECTORY --out REPORT.html` to refresh this file "
           "(add --show-text if the report was built with it, as the bundled sample's is).")
STATUSES = ("Reviewer statuses: pending = not reviewed yet; accepted = the change is real and acceptable; "
            "follow_up = needs more work before deciding; dismissed = not a real change (for example a gold-label error).")
KINDS = {"stable_regression": "Stable regression", "stable_improvement": "Stable improvement"}


def percent(value):
    return "unavailable" if value is None else f"{100 * value:.1f}%"


def plural(count, word="call"):
    return f"{count} {word}{'' if count == 1 else 's'}"


def join_clauses(clauses):
    return clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " and " + clauses[-1]


def parsed_text(counts, scope=""):
    """'6 of 12 answers parse and 5 are correct', with every denominator stated."""
    if not counts.get("available", True):
        return "not recorded (" + counts.get("reason", "no reply text") + ")"
    text = f"{counts['parsed']} of {counts['answers']}{scope} parse"
    if counts["gold"] and counts["gold"] == counts["answers"]:
        text += f" and {counts['correct']} are correct"
    elif counts["gold"]:
        text += f", and {counts['correct']} of the {counts['gold']} with gold are correct"
    return text


def self_correction_text(row):
    """The self-correction hazard in short plain sentences, or None when no call had one.

    Each parse policy is reported on its own, over the same answers, and never mixed.
    """
    calls = row.get("self_correction", 0)
    if calls is None:
        return ("Self-correction: not recorded for the archived arm (its archive predates these counts), "
                "so a self-correction in the other arm is not evidence that this arm never had one.")
    if not calls:
        return None
    sentences = [f"Self-correction: {plural(calls)} answered, then wrote a second JSON answer in the same reply.",
                 "A strict parser expects one JSON object and reads no answer from such a reply."]
    outcomes = row.get("self_correction_outcomes")
    if outcomes:
        clauses = [plural(outcomes[key]) + text for key, text in (
            ("retry_parsed_calls", " parsed on a retry"),
            ("unparsed_calls", " ended with no parseable reply"),
            ("other_failure_calls", " ended in another failure (see the call errors)")) if outcomes[key]]
        if clauses:
            sentences.append("As recorded, " + join_clauses(clauses) + ".")
        policies = outcomes["policies"]
        sentences.append("Strict, as recorded: " + parsed_text(policies["strict"], " answers in these calls") + ".")
        for policy, name in (("last_object", "Last-object recovery"), ("object_merge", "Object-merge recovery")):
            if policy in policies:
                sentences.append(name + ": " + parsed_text(policies[policy]) + ".")
    return " ".join(sentences)


def policy_table(data):
    """Parse success and accuracy per arm under each named parse policy, as finished text.

    Returns {"header", "rows", "notes"} so the page, the JSON and the Markdown show the same cells.
    """
    policies = data.get("parse_policies")
    if not policies:
        return None
    arms, applicable = policies["arms"], policies["applicable"]
    base = arms["old"]["strict"]
    has_gold = base["gold"] > 0
    scope = f"each model: {plural(base['calls'])}, {plural(base['answers'], 'answer')}"
    if has_gold:
        scope += ", all with gold" if base["gold"] == base["answers"] else f", {base['gold']} with gold"
    header = [f"Measure ({scope})", "old", "new"]

    def cell(arm, policy, numerator, denominator, rate):
        counts = arms[arm][policy]
        if not counts.get("available", True):
            return "not recorded"
        if not counts[denominator]:
            return "none"
        text = f"{counts[numerator]} of {counts[denominator]}"
        return text + f" ({percent(counts[rate])})" if rate else text

    def row(label, policy, numerator, denominator, rate=None):
        return [label] + [cell(arm, policy, numerator, denominator, rate) for arm in ("old", "new")]

    rows = [row("Strict parse: calls with every answer parsed", "strict", "calls_parsed", "calls"),
            row("Strict parse: answers parsed", "strict", "parsed", "answers")]
    names = {"strict_first_attempt": "strict, first reply only (no retries)",
             "last_object": "last-object recovery", "object_merge": "object-merge recovery (later answers win)"}
    if has_gold:
        rows += [row("Strict accuracy: correct of all gold answers (a missing or off-list answer counts wrong)",
                     "strict", "correct", "gold", "accuracy"),
                 row("Accuracy among strictly parsed answers only", "strict", "correct", "parsed_gold",
                     "parsed_accuracy")]
        rows += [row("Accuracy, " + names[policy], policy, "correct", "gold", "accuracy")
                 for policy in applicable[1:]]
    else:
        rows += [row("Answers parsed, " + names[policy], policy, "parsed", "answers") for policy in applicable[1:]]
    retries = policies.get("retries")
    retry_text = "a reply it cannot read is retried" + (f", up to {plural(retries, 'time')}" if retries is not None else "")
    definition = ("Parse policies. Strict is the recorded run. " +
                  ("It reads one JSON object per reply, and " if len(applicable) > 2 else "In it, ") +
                  retry_text + ". First reply only scores each call's first reply, with no retries.")
    if len(applicable) > 2:
        definition += (" Last-object recovery takes the last complete JSON answer object in a reply the strict "
                       "parser could not read. Object-merge recovery merges every JSON answer object in that "
                       "reply, later answers winning. Both read the stored replies in order and stop at the "
                       "first that yields an answer, so neither makes a new call. The run's own answers stay strict.")
    notes = [definition]
    if has_gold and any(arms[arm]["strict"]["parsed"] < arms[arm]["strict"]["answers"] for arm in ("old", "new")):
        notes.append("Strict accuracy counts a missing answer as wrong. If a recovery row closes most of a gap, "
                     "most of that gap is answers the strict parser could not read, not wrong answers.")
    return {"header": header, "rows": rows, "notes": notes}


def summary_lines(data):
    """Headline numbers as finished text, so the Python and browser exports print the same thing."""
    hazards = data["hazard_summary"]
    lines = ["Format hazards (final answers, as recorded): " + "; ".join(
        f"{arm} {hazards[arm]['parse_failures']} missing or unparseable, {hazards[arm]['off_list']} off-list"
        for arm in ("old", "new")) + f", out of {data['n_items'] * data['draws']} per model."]
    table = data.get("parse_policy_table") or policy_table(data)
    paragraphs = []
    if table:
        lines += ["", "| " + " | ".join(table["header"]) + " |", "| --- | --- | --- |"]
        lines += ["| " + " | ".join(row) + " |" for row in table["rows"]]
        paragraphs += table["notes"][:1]
    if data.get("gold"):
        gold = data["gold"]
        paragraphs.append(f"Strict accuracy on gold: old {percent(gold['old_accuracy'])}, new "
                          f"{percent(gold['new_accuracy'])} (D* {100 * gold['Dstar']:+.1f} percentage points, "
                          "new minus old).")
    if table:
        paragraphs += table["notes"][1:]
    for arm in ("old", "new"):
        text = self_correction_text(hazards[arm])
        if text:
            paragraphs.append(f"{arm}: {text}")
    # Blank lines keep each sentence group its own Markdown paragraph.
    for paragraph in paragraphs:
        lines += ["", paragraph]
    return lines


def simulated_arms(models):
    """Arms answered by the offline mock provider, so reports can say so."""
    return [arm for arm in ("old", "new") if str(models.get(arm, "")).partition(":")[0] == "mock"]


def private_view(analysis, show_text=False, salt=None):
    # JSON round-trip also separates shared count lists in the queue and item table.
    data = json.loads(json.dumps(analysis))
    if show_text or data.get("answers_hashed"):
        return data
    salt = salt if salt is not None else secrets.token_bytes(32)
    labels = set(data["labels"])

    def answer(value):
        return answer_key(value, labels, salt)

    for row in data["unlabelled"]["items"] + data["review_queue"] + data.get("tool_confusion", []):
        for key in ("old_mode", "new_mode", "gold"):
            if key in row:
                row[key] = answer(row[key])
        for key in ("old_counts", "new_counts"):
            for entry in row.get(key, []):
                entry["answer"] = answer(entry["answer"])
    return data


def decision_record(data):
    record = {"schema_version": 1, "audit_id": data["audit_id"], "created_at": data["created_at"], "models": data["models"],
            "draws": data["draws"], "batch": data["batch"],
            "decision": "pending human review", "reviewer": "", "notes": "", "summary": summary_lines(data)}
    if data.get("archived_old"):
        for key in ("archived_old", "prompt_changes", "requested_draws", "draws_notice"):
            record[key] = copy.deepcopy(data[key])
    if data.get("simulated"):
        record["simulated"] = True
    if data.get("answer") == "tool":
        record["tool_choice_hazards"] = {arm: {key: data["hazard_summary"][arm][key] for key in TOOL_HAZARDS}
                                         for arm in ("old", "new")}
    if data.get("parse_policies"):
        record["parse_policies"] = copy.deepcopy(data["parse_policies"])
    record["items"] = copy.deepcopy(data["review_queue"])
    return record


def plain(value):
    """One Markdown line: whitespace runs collapsed and HTML-active characters escaped.

    Keeps arbitrary multiline IDs, labels and notes on a single line. Must match the
    browser export in report.html character for character.
    """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = re.sub(r"^ +| +$", "", re.sub(r"[\r\n\t ]+", " ", text))
    return html.escape(text, quote=False)


def shown_answer(value):
    return "[failed / missing]" if value is None else str(value)


def answer_counts(rows):
    return ", ".join(shown_answer(row["answer"]) + " × " + str(row["count"]) for row in rows)


def item_kind(item, draws):
    kind = item.get("kind")
    if kind in KINDS:
        return (f"{KINDS[kind]}: old correct in {item['old_correct']} of {draws} draws, "
                f"new correct in {item['new_correct']} of {draws}.")
    if kind == "CHANGED":
        return ("Changed (no gold): the answer distributions differ beyond run-to-run noise. Most common answer: "
                "old " + plain(shown_answer(item.get("old_mode"))) + ", new " +
                plain(shown_answer(item.get("new_mode"))) + ".")
    return plain(kind)


def markdown(record):
    filled = lambda value: plain(value) or "(not filled in)"
    lines = ["# Migration decision record", "", "Decision: " + filled(record["decision"]),
             "Reviewer: " + filled(record.get("reviewer", "")), "Notes: " + filled(record.get("notes", "")),
             "", "Old: " + plain(record["models"]["old"]), "New: " + plain(record["models"]["new"]),
             f"Draws: {record['draws']}; batch: {record['batch']}", ""]
    for line in record.get("summary", []):
        lines.append(plain(line))
    if record.get("summary"):
        lines.append("")
    lines += ["Queued items need human review. This record is not a migration approval.", "",
              FILL_IN, "", STATUSES, ""]
    quote = plain
    if record.get("archived_old"):
        archived = record["archived_old"]
        lines[6] += " (ARCHIVED)"
        lines[1:1] = ["", "Old arm: ARCHIVED", "Archive head: " + quote(archived["head_hash"]),
                      "Archive created: " + quote(archived["created_at"]),
                      "Source captured: " + quote(archived["source_created_at"]),
                      "Original served models: " + quote(archived["served_models"])]
        if record.get("prompt_changes"):
            lines[1:1] = ["", "WARNING: Prompt differs from archive: " + quote(record["prompt_changes"])]
        if record.get("draws_notice"):
            lines[1:1] = ["", record["draws_notice"]]
    if record.get("simulated"):
        lines[1:1] = ["", SIMULATED_RECORD]
    if "tool_choice_hazards" in record:
        lines.extend(["## Tool-choice hazards", "", "Review these before tool-choice changes.", ""])
        for arm, hazards in record["tool_choice_hazards"].items():
            lines.append(arm + ": " + "; ".join(key.replace("_", " ") + ": " + str(value) for key, value in hazards.items()))
        lines.append("")
    for item in record["items"]:
        lines.extend(["## Item " + plain(item["id"]), "", item_kind(item, record["draws"])])
        if item.get("gold") is not None:
            lines.append("Gold: " + plain(item["gold"]))
        if item.get("input") is not None:
            lines.append("Input: " + plain(item["input"]))
        lines.extend(["Old answers: " + plain(answer_counts(item["old_counts"])),
                      "New answers: " + plain(answer_counts(item["new_counts"])),
                      "Reviewer status: " + plain(item["reviewer_status"]),
                      "Reviewer notes: " + filled(item.get("reviewer_notes", "")), ""])
    if not record["items"]:
        lines.append("No queued items. An empty queue does not establish migration suitability.")
    return "\n".join(lines) + "\n"


def build_report(runs, output, show_text=False):
    manifest, rows, calls = load_run(runs)
    salt = read_salt(runs)
    data = private_view(analyze(manifest, rows, calls, show_text=show_text), show_text, salt)
    audit_id = manifest.get("audit_id") or hashlib.sha256(
        json.dumps([manifest["created_at"], manifest["models"]], sort_keys=True).encode()).hexdigest()
    data["audit_id"] = audit_id
    data["version"] = __version__
    # The version that captured the answers can differ from the one building this report.
    data["capture_version"] = manifest.get("version")
    # Finished text, so the page shows the same sentence as the decision exports.
    data["self_correction_notes"] = {arm: text for arm in ("old", "new")
                                     if (text := self_correction_text(data["hazard_summary"][arm]))}
    data["parse_policy_table"] = policy_table(data)
    simulated = simulated_arms(manifest["models"])
    if simulated:
        data["simulated"] = True
        data["warnings"].insert(0, "SIMULATED: the " + " and ".join(simulated) +
                                (" arms use" if len(simulated) > 1 else " arm uses") +
                                " the offline mock provider. No model was called for " +
                                ("them" if len(simulated) > 1 else "it") +
                                "; these answers illustrate the report and are not evidence about any model.")
    if show_text:
        local_calls = copy.deepcopy(calls)
        # Arguments are local-capture-only, even when text export is requested.
        for call in local_calls:
            for attempt in call["attempts"]:
                for tool_call in attempt.get("tool_calls", []):
                    tool_call.pop("arguments", None)
        data["local_text"] = {"inputs": rows, "system": manifest["system"], "template": manifest["template"],
                              "calls": local_calls}
    else:
        data["text_notice"] = "Raw outputs and inputs are hidden. Allowed labels are readable; other answers use per-run pseudonyms, not anonymisation. Rebuild with --show-text to include local text."
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    decision_path = output.with_suffix(".decision.json")
    record = decision_record(data)
    if show_text:
        texts = {row["id"]: item_text(row) for row in rows}
        for item in record["items"]:
            item["input"] = texts.get(item["id"])
    if decision_path.exists():
        previous = json.loads(decision_path.read_text(encoding="utf-8-sig"))
        same_audit = (previous.get("audit_id") == record["audit_id"] if previous.get("audit_id") else
                      previous.get("created_at") == record["created_at"] and previous.get("models") == record["models"])
        if not same_audit:
            raise ValueError(f"{decision_path} belongs to a different audit; choose a new --out to preserve reviewer work")
        if same_audit:
            for field in ("decision", "reviewer", "notes"):
                record[field] = previous.get(field, record[field])
            statuses = {(item["id"], item["kind"]): item for item in previous.get("items", [])}
            for item in record["items"]:
                old = statuses.get((item["id"], item["kind"]), {})
                item["reviewer_status"] = old.get("reviewer_status", "pending")
                item["reviewer_notes"] = old.get("reviewer_notes", "")
    data["decision_record"] = record
    data["export_stem"] = output.stem + "." + audit_id[:12] + ".decision"
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = Path(__file__).with_name("report.html").read_text(encoding="utf-8-sig")
    output.write_text(template.replace("__AUDIT_DATA__", payload), encoding="utf-8")
    output.with_suffix(".analysis.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    decision_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".decision.md").write_text(markdown(record), encoding="utf-8")
    return {"report": str(output), "decision_json": str(decision_path),
            "decision_markdown": str(output.with_suffix(".decision.md")), "queued_items": len(record["items"])}
