"""Audit summaries. Raw provider replies never enter the analysis object."""

from collections import Counter
import json
import math
from pathlib import Path

from .protocol import answer_objects, keyed_answers, normalize, pattern, read_inputs, recover_keyed, validate_gold
from .privacy import answer_key
from .providers import model_ids, safe_tool_error, split_model
from .stats import ALPHA, PERMUTATIONS, bh, distribution_test, graded_test
from .tools import REPLY_FAILURES, STOP_HAZARDS, TOOL_HAZARDS

# Parse policies, each scored separately and never mixed. "strict" is the recorded run.
STRICT_POLICIES = ("strict", "strict_first_attempt")
RECOVERY_POLICIES = ("last_object", "object_merge")
POLICY_LABELS = {"strict": "Strict (the recorded run, with its retries)",
                 "strict_first_attempt": "Strict, first reply only (no retries)",
                 "last_object": "Last-object recovery",
                 "object_merge": "Object-merge recovery (later answers win)"}


UNRECORDED_POLICY = {"available": False, "reason": "the archive holds no reply text for this arm"}


class Unrecorded(Exception):
    """The reply text a recovery policy needs was not kept (an archive made without raw text)."""


def recovered_answers(attempt, policy, size, archived):
    """(answers, already_pseudonymised) that a recovery policy reads from one stored reply."""
    raw = attempt.get("raw_text")
    # An archive made without raw text replays each reply as "", which is not an empty reply.
    if isinstance(raw, str) and (raw or not archived):
        if policy == "last_object":
            objects = answer_objects(raw, size)
            return (keyed_answers(objects[-1], size) if objects else [None] * size), False
        return recover_keyed(raw, size)[1], False
    if attempt.get("json_objects") == 0:
        return [None] * size, False
    if policy == "object_merge" and attempt.get("recovered_answers") is not None:
        return list(attempt["recovered_answers"]), archived
    raise Unrecorded


def policy_answers(policy, call, recorded, size, comparable):
    """One call's answers under a parse policy, replaying the stored replies in order.

    Strict is the recorded outcome. A recovery policy reads each stored reply in turn
    and stops at the first that yields an answer, as the run's retries did; it reads a
    reply's objects only where the strict parser read nothing, so it never makes a call.
    """
    if call is None:
        return [None] * size
    attempts = call["attempts"]
    if policy == "strict":
        return list(recorded)
    if policy == "strict_first_attempt":
        # The runner retries only a reply that yielded no answer at all.
        return list(recorded) if len(attempts) <= 1 else [None] * size
    for index, attempt in enumerate(attempts):
        strict = recorded if index == len(attempts) - 1 else [None] * size
        if any(value is not None for value in strict):
            return list(strict)
        if attempt.get("error") in (None, "unparseable"):
            values, encoded = recovered_answers(attempt, policy, size, bool(call.get("archived")))
            values = [value if encoded or value is None else comparable(value) for value in values]
            if any(value is not None for value in values):
                return values
        if attempt.get("error") in STOP_HAZARDS + ("unknown_tool",):
            break
    return [None] * size


def policy_counts():
    return dict.fromkeys(("calls", "calls_parsed", "answers", "parsed", "gold", "parsed_gold", "correct"), 0)


def finish_counts(counts):
    """Rates with their numerators and denominators; None where the denominator is 0."""
    counts["accuracy"] = counts["correct"] / counts["gold"] if counts["gold"] else None
    counts["parsed_accuracy"] = counts["correct"] / counts["parsed_gold"] if counts["parsed_gold"] else None
    counts["available"] = True
    return counts


def distribution(values):
    return [{"answer": answer, "count": count} for answer, count in
            sorted(Counter(values).items(), key=lambda pair: (-pair[1], pair[0] is not None, pair[0] or ""))]


def load_run(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported run schema")
    if manifest.get("archive_replay"):
        manifest["_answer_salt"] = (directory / "privacy-salt").read_bytes().hex()
    rows = read_inputs(directory / "inputs.jsonl")
    validate_gold(rows, manifest["labels"], "tool names plus 'none'" if manifest.get("answer") == "tool" else "labels.txt")
    if manifest["n_items"] != len(rows) or min(manifest["draws"], manifest["batch"]) < 1:
        raise ValueError("Run manifest and input dimensions disagree")
    calls_path = directory / "calls.jsonl"
    calls = []
    if calls_path.exists():
        lines = calls_path.read_bytes().splitlines()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                calls.append(json.loads(line.decode("utf-8-sig")))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                detail = f"{exc.msg}, column {exc.colno}" if isinstance(exc, json.JSONDecodeError) else "invalid UTF-8"
                if number == len(lines):
                    manifest.setdefault("load_warnings", []).append(
                        f"Incomplete run: ignored torn calls.jsonl line {number}: {detail}.")
                    manifest["complete"] = False
                else:
                    raise ValueError(f"Invalid JSON in calls.jsonl line {number}: {detail}") from exc
    seen = set()
    for call in calls:
        key = call["arm"], call["draw"], call["batch"]
        if key in seen or key[0] not in ("old", "new") or not 0 <= key[1] < manifest["draws"]:
            raise ValueError("Duplicate or invalid call coordinates")
        expected = [row["id"] for row in rows[key[2] * manifest["batch"]:(key[2] + 1) * manifest["batch"]]]
        if not expected or expected != call["item_ids"] or expected != [p["id"] for p in call["predictions"]]:
            raise ValueError("Run batch IDs do not match the input snapshot")
        seen.add(key)
    return manifest, rows, calls


def analyze(manifest, rows, calls, show_text=False):
    r, n, labels = manifest["draws"], len(rows), manifest["labels"]
    tool_mode = manifest.get("answer") == "tool"
    hazard_keys = TOOL_HAZARDS if tool_mode else STOP_HAZARDS
    salt = bytes.fromhex(manifest["_answer_salt"]) if manifest.get("_answer_salt") else None

    def comparable(value):
        value = normalize(value, labels)
        return answer_key(value, labels, salt) if salt is not None else value

    ranks = {arm: {} for arm in ("old", "new")}
    if salt is not None:
        for arm in ranks:
            values = sorted({normalize(p["answer"], labels) for c in calls if c["arm"] == arm and not c.get("archived")
                             for p in c["predictions"] if p["answer"] is not None})
            ranks[arm] = {comparable(value): i for i, value in enumerate(values)}
        for call in calls:
            if call.get("archived"):
                ranks[call["arm"]].update({p["answer"]: p["answer_rank"] for p in call["predictions"]
                                          if p["answer"] is not None})

    def counts(values, arm):
        if salt is None:
            return distribution(values)
        return [{"answer": value, "count": count} for value, count in sorted(
            Counter(values).items(), key=lambda pair: (-pair[1], pair[0] is not None,
                                                      ranks[arm].get(pair[0], -1)))]
    validate_gold(rows, labels, "tool names plus 'none'" if tool_mode else "labels.txt")
    index = {row["id"]: i for i, row in enumerate(rows)}
    answers = {arm: [[None] * n for _ in range(r)] for arm in ("old", "new")}
    call_map = {(c["arm"], c["draw"], c["batch"]): c for c in calls}
    hazards, warnings, served = [], list(manifest.get("load_warnings", [])), {}
    archive_replay = manifest.get("archive_replay")
    archived_old = None
    if archive_replay:
        frozen = archive_replay["manifest"]
        archived_old = {"archived": True, "status": "ARCHIVED", "head_hash": archive_replay["head_hash"],
                        "created_at": frozen["created_at"], "source_created_at": frozen["source_created_at"],
                        "source_audit_id": frozen.get("source_audit_id"), "model": frozen["model"],
                        "served_models": frozen["served_models"]}
        if archive_replay.get("prompt_changes"):
            warnings.insert(0, "WARNING: Prompt differs from archive (" +
                            ", ".join(archive_replay["prompt_changes"]) +
                            "); --allow-prompt-change permits a prompt-confounded comparison.")
        warnings.append(f"Old arm ARCHIVED: {archived_old['head_hash']} (archive created {archived_old['created_at']}; "
                        f"source captured {archived_old['source_created_at']}); no provider call was made for the old arm.")
    if manifest.get("draws_notice"):
        warnings.append(manifest["draws_notice"])
    recovery = {arm: dict.fromkeys(("calls", "items", "on_list", "gold_items", "correct", "retry_parsed",
                                    "other_failure", "unrecorded"), 0) for arm in answers}
    applicable = STRICT_POLICIES + (RECOVERY_POLICIES if not tool_mode and manifest["batch"] > 1 else ())
    policies = {arm: {policy: policy_counts() for policy in applicable} for arm in answers}
    # The same counts restricted to calls with a self-correction, plus how their retries ended.
    corrected = {arm: {"calls": 0, "answers": 0, "gold": 0, "retry_parsed_calls": 0, "unparsed_calls": 0,
                       "other_failure_calls": 0,
                       "policies": {policy: policy_counts() for policy in ("strict",) + applicable[2:]}}
                 for arm in answers}
    unrecorded_policy = {arm: set() for arm in answers}

    def is_correct(value, i):
        return (value is not None and (not labels or value in labels) and "gold" in rows[i]
                and value == comparable(rows[i]["gold"]))

    def tally(counts, values, start):
        counts["calls"] += 1
        counts["calls_parsed"] += int(all(value is not None for value in values))
        for offset, value in enumerate(values):
            counts["answers"] += 1
            counts["parsed"] += int(value is not None)
            if "gold" in rows[start + offset]:
                counts["gold"] += 1
                counts["parsed_gold"] += int(value is not None)
                counts["correct"] += int(is_correct(value, start + offset))

    for arm in answers:
        is_archived = arm == "old" and archived_old is not None
        requested = split_model(manifest["models"][arm])[1]
        served_ids = set()
        missing_served = 0
        errors = Counter()
        for draw in range(r):
            for batch, start in enumerate(range(0, n, manifest["batch"])):
                size = min(manifest["batch"], n - start)
                call = call_map.get((arm, draw, batch))
                predictions = call["predictions"] if call else [{"answer": None}] * size
                patterns = Counter()
                for offset, pred in enumerate(predictions):
                    answer = pred["answer"]
                    answers[arm][draw][start + offset] = answer if call and call.get("archived") else comparable(answer)
                    if tool_mode:
                        hazard = "parse_failure" if answer is None else "unknown_tool" if answer not in labels else None
                    else:
                        hazard = pred.get("pattern") if call and call.get("archived") else pattern(answer, labels)
                    if hazard:
                        patterns[hazard] += 1
                attempts = call["attempts"] if call else []
                for attempt in attempts:
                    ids = model_ids(attempt.get("served_models"))
                    served_ids.update(ids)
                    missing_served += not bool(ids)
                    if attempt.get("error"):
                        error = safe_tool_error(attempt["error"]) if tool_mode and not show_text else str(attempt["error"])
                        errors[error[:250]] += 1
                hazards.append({"arm": arm, "draw": draw, "batch": batch, "n_items": size,
                                "parse_failures": patterns["parse_failure"],
                                "off_list": sum(v for k, v in patterns.items() if k != "parse_failure"),
                                "patterns": dict(patterns), "dominant_pattern": patterns.most_common(1)[0][0] if patterns else "none",
                                "attempts": len(attempts), "retries": max(0, len(attempts) - 1),
                                "failed_attempts": sum(bool(a["error"]) or a["parse_failures"] > 0 for a in attempts),
                                "attempt_missing_answers": sum(a["parse_failures"] for a in attempts),
                                "missing_call": call is None})
                hazards[-1].update({key: int(bool(call and call.get(key))) for key in hazard_keys})
                if tool_mode:
                    # Unknown names are categorical failures, including any later call in a multi-call reply.
                    if patterns["unknown_tool"] or (call and call.get("unknown_tool_names")):
                        hazards[-1]["unknown_tool"] = 1
                    tool_patterns = {key: hazards[-1][key] for key in TOOL_HAZARDS if hazards[-1][key]}
                    if patterns["parse_failure"]:
                        tool_patterns["parse_failure"] = patterns["parse_failure"]
                    hazards[-1]["patterns"] = tool_patterns
                    hazards[-1]["dominant_pattern"] = max(tool_patterns, key=tool_patterns.get) if tool_patterns else "none"
                else:
                    hazards[-1]["patterns"].update({key: hazards[-1][key] for key in STOP_HAZARDS if hazards[-1][key]})
                # Two or more JSON answer objects in one reply. Older live captures lack the
                # key and count 0; an archived arm whose archive did not record it is None.
                unrecorded = (is_archived and not tool_mode and manifest["batch"] > 1
                              and any("json_objects" not in a for a in attempts))
                hazards[-1]["self_correction"] = None if unrecorded else int(any(a.get("self_correction") for a in attempts))
                recovery[arm]["unrecorded"] += int(unrecorded)
                final = attempts[-1] if attempts else {}
                if hazards[-1]["self_correction"] and not final.get("self_correction"):
                    # A later attempt ended the call: either a retry parsed or it failed another way.
                    parsed = not final.get("error") and final.get("parse_failures", size) < size
                    recovery[arm]["retry_parsed" if parsed else "other_failure"] += 1
                if final.get("self_correction"):
                    # Descriptive only: what merging the objects would have answered.
                    merged = final.get("recovered_answers") or []
                    recovery[arm]["calls"] += 1
                    recovery[arm]["items"] += size
                    for offset in range(size):
                        value = merged[offset] if offset < len(merged) else None
                        if not isinstance(value, str) or (labels and value not in labels):
                            continue
                        recovery[arm]["on_list"] += 1
                        row = rows[start + offset]
                        if "gold" in row:
                            # Archived answers are already pseudonymised with the run's salt.
                            mine = value if call.get("archived") else comparable(value)
                            recovery[arm]["gold_items"] += 1
                            recovery[arm]["correct"] += int(mine == comparable(row["gold"]))
                recorded = answers[arm][draw][start:start + size]
                outcome = {}
                for policy in applicable:
                    if policy in unrecorded_policy[arm]:
                        continue
                    try:
                        outcome[policy] = policy_answers(policy, call, recorded, size, comparable)
                    except Unrecorded:
                        unrecorded_policy[arm].add(policy)
                        continue
                    tally(policies[arm][policy], outcome[policy], start)
                if hazards[-1]["self_correction"]:
                    group = corrected[arm]
                    group["calls"] += 1
                    group["answers"] += size
                    group["gold"] += sum("gold" in rows[start + offset] for offset in range(size))
                    if any(value is not None for value in recorded):
                        group["retry_parsed_calls"] += 1
                    elif final.get("error") in (None, "unparseable"):
                        group["unparsed_calls"] += 1
                    else:
                        group["other_failure_calls"] += 1
                    for policy, subset in group["policies"].items():
                        if policy in outcome:
                            tally(subset, outcome[policy], start)
        served[arm] = sorted(served_ids)
        if not is_archived and served_ids - {requested}:
            warnings.append(f"Served-model mismatch for {arm}: requested {requested}; served " + ", ".join(sorted(served_ids)) + ". Aliases may resolve to dated IDs; verify this pair.")
        if missing_served and not is_archived:
            warnings.append(f"{arm}: {missing_served} attempts have no served-model ID. Identity could not be verified for those attempts.")
        if errors:
            warnings.append(f"{arm} call errors: " + "; ".join(f"{text} ({count} attempts)" for text, count in errors.items()))
        arm_calls = [c for c in calls if c["arm"] == arm]
        if not is_archived and arm_calls and all(all(a.get("error") and a["error"] not in REPLY_FAILURES for a in c["attempts"]) for c in arm_calls):
            warnings.append(f"{arm}: every collected call failed at the provider. No behavior comparison is available for this arm; check credentials, model ID and URL.")
    if not manifest.get("complete") or any(h["missing_call"] for h in hazards):
        warnings.append("Incomplete run: missing calls are retained as failed answers. Re-run into a fresh directory for a complete audit.")
    if r < 2:
        warnings.append("Only one draw: self-disagreement cannot be estimated and repeated-draw evidence is unavailable.")
    gold_indices = [i for i, row in enumerate(rows) if "gold" in row]
    unlabelled_indices = [i for i, row in enumerate(rows) if "gold" not in row]
    minimum_p = 2 / math.comb(2 * r, r)
    recommended = 5
    while 2 / math.comb(2 * recommended, recommended) > float(ALPHA) / max(1, len(unlabelled_indices)):
        recommended += 1
    power = {"minimum_per_item_p": minimum_p, "isolated_bh_threshold": float(ALPHA) / max(1, len(unlabelled_indices)),
             "monte_carlo_resolution": 1 / (PERMUTATIONS + 1),
             "recommended_draws": recommended, "no_item_can_reach_significance": minimum_p > float(ALPHA)}
    if unlabelled_indices:
        text = f"Draw power: R={r}; smallest achievable per-item exact p is {minimum_p:.6g}. "
        if power["no_item_can_reach_significance"]:
            text += "No item can reach BH significance at 0.05, even with completely separated answers. "
        elif minimum_p > power["isolated_bh_threshold"]:
            text += "An isolated changed item cannot reach BH significance; several items must change together. "
        text += f"Recommend at least R={recommended} for draw resolution among {len(unlabelled_indices)} tests; noisy changes may need more draws. "
        text += f"Monte Carlo p resolution is {power['monte_carlo_resolution']:.6g}."
        if power["monte_carlo_resolution"] > power["isolated_bh_threshold"]:
            text += " At this permutation budget, no isolated item can reach BH significance regardless of R; a larger permutation budget is also required."
        warnings.append(text)
    correct = {arm: [[int(answers[arm][draw][i] is not None and
                              (not labels or answers[arm][draw][i] in labels) and
                              answers[arm][draw][i] == comparable(rows[i]["gold"])) for i in gold_indices]
                     for draw in range(r)] for arm in answers}
    strips = {}
    for arm in answers:
        other = "new" if arm == "old" else "old"
        strips[arm] = [{"draw": draw,
                        "metric": "accuracy" if gold_indices else "paired agreement",
                        "value": sum(correct[arm][draw]) / len(gold_indices) if gold_indices else
                        sum(answers[arm][draw][i] is not None and answers[arm][draw][i] == answers[other][draw][i]
                            and (not labels or answers[arm][draw][i] in labels) for i in range(n)) / n,
                        "parse_failures": sum(h["parse_failures"] for h in hazards if h["arm"] == arm and h["draw"] == draw),
                        "off_list": sum(h["off_list"] for h in hazards if h["arm"] == arm and h["draw"] == draw)} for draw in range(r)]
    floors = {}
    for arm in strips:
        for draw in strips[arm]:
            draw.update({key: sum(h[key] for h in hazards if h["arm"] == arm and h["draw"] == draw["draw"])
                         for key in hazard_keys})
    for arm in answers:
        counters = [Counter(answers[arm][d][i] for d in range(r)) for i in range(n)]
        floors[arm] = {"mean_flip_rate": sum(r - max(c.values()) for c in counters) / (r * n),
                       "items_ever_flipping": sum(len(c) > 1 for c in counters),
                       "pairwise_disagreement": (sum(count * (r - count) for c in counters for count in c.values()) /
                                                 (n * r * (r - 1))) if r > 1 else None}
    queue, gold_result = [], None
    if gold_indices:
        ki = [sum(d[j] for d in correct["old"]) for j in range(len(gold_indices))]
        kc = [sum(d[j] for d in correct["new"]) for j in range(len(gold_indices))]
        gold_result = {"n_items": len(gold_indices), "Dstar": (sum(kc) - sum(ki)) / (r * len(gold_indices)),
                       "Dstar_numerator": sum(kc) - sum(ki), "Dstar_denominator": r * len(gold_indices),
                       "old_accuracy": sum(ki) / (r * len(gold_indices)),
                       "new_accuracy": sum(kc) / (r * len(gold_indices)),
                       "primary_min_p": None}
        if any("slice" in rows[i] for i in gold_indices):
            memberships = [rows[i].get("slice", "(unsliced)") for i in gold_indices]
            gold_result["primary_min_p"] = graded_test(correct["old"], correct["new"], memberships)
        for j, i in enumerate(gold_indices):
            worse = 10 * ki[j] >= 8 * r and 10 * kc[j] <= 2 * r
            better = 10 * kc[j] >= 8 * r and 10 * ki[j] <= 2 * r
            if worse or better:
                queue.append({"id": rows[i]["id"], "slice": rows[i].get("slice"),
                              "gold": comparable(rows[i]["gold"]) if salt is not None else rows[i]["gold"],
                              "kind": "stable_regression" if worse else "stable_improvement",
                              "expected_drop": (ki[j] - kc[j]) / r, "old_correct": ki[j], "new_correct": kc[j],
                              "old_counts": counts([answers["old"][d][i] for d in range(r)], "old"),
                              "new_counts": counts([answers["new"][d][i] for d in range(r)], "new"),
                              "reviewer_status": "pending", "reviewer_notes": ""})
        queue.sort(key=lambda row: (-row["expected_drop"], row["id"]))
    changed_items, raw_p = [], []
    for i in unlabelled_indices:
        old = [answers["old"][d][i] for d in range(r)]
        new = [answers["new"][d][i] for d in range(r)]
        old_mode, new_mode = counts(old, "old")[0]["answer"], counts(new, "new")[0]["answer"]
        statistic, p, exact = distribution_test(old, new)
        raw_p.append(p)
        changed_items.append({"id": rows[i]["id"], "slice": rows[i].get("slice"), "old_mode": old_mode,
                              "new_mode": new_mode, "old_counts": counts(old, "old"), "new_counts": counts(new, "new"),
                              "statistic_numerator": statistic, "statistic_denominator": 2 * r,
                              "p": float(p), "exact_enumerated_p": float(exact) if exact is not None else None})
    for item, adjusted in zip(changed_items, bh(raw_p)):
        item["adjusted_p"] = float(adjusted)
        item["changed"] = item["old_mode"] != item["new_mode"] and adjusted <= ALPHA
        if item["changed"]:
            queue.append({**item, "kind": "CHANGED", "reviewer_status": "pending", "reviewer_notes": ""})
    summary = {}
    for arm in answers:
        arm_rows = [h for h in hazards if h["arm"] == arm]
        patterns = Counter()
        for row in arm_rows:
            patterns.update(row["patterns"])
        summary[arm] = {key: sum(row[key] for row in arm_rows) for key in
                        ("parse_failures", "off_list", "retries", "failed_attempts", "attempt_missing_answers")}
        summary[arm]["patterns"] = dict(patterns)
        summary[arm]["dominant_pattern"] = patterns.most_common(1)[0][0] if patterns else "none"
        summary[arm].update({key: sum(row[key] for row in arm_rows) for key in hazard_keys})
        # Calls, not items, so it stays out of patterns; None when an archive did not record it.
        summary[arm]["self_correction"] = (None if recovery[arm]["unrecorded"] else
                                           sum(row["self_correction"] for row in arm_rows))
        summary[arm]["self_correction_recoverable_items"] = recovery[arm]
        group = corrected[arm]
        group["policies"] = {policy: (UNRECORDED_POLICY.copy() if policy in unrecorded_policy[arm]
                                      else finish_counts(subset)) for policy, subset in group["policies"].items()}
        summary[arm]["self_correction_outcomes"] = None if recovery[arm]["unrecorded"] else group
    policy_result = {"retries": manifest.get("retries"), "applicable": list(applicable),
                     "labels": {policy: POLICY_LABELS[policy] for policy in applicable},
                     "not_applicable": ({} if len(applicable) > 2 else
                                        {policy: "replies are not batched JSON objects (tool mode or batch 1)"
                                         for policy in RECOVERY_POLICIES}),
                     "arms": {arm: {policy: (UNRECORDED_POLICY.copy() if policy in unrecorded_policy[arm]
                                             else finish_counts(policies[arm][policy])) for policy in applicable}
                              for arm in answers}}
    confusion = Counter((counts([answers["old"][d][i] for d in range(r)], "old")[0]["answer"],
                         counts([answers["new"][d][i] for d in range(r)], "new")[0]["answer"]) for i in range(n)) if tool_mode else Counter()
    return {"schema_version": 1, "created_at": manifest["created_at"], "models": manifest["models"],
            **({"archived_old": archived_old, "prompt_changes": archive_replay.get("prompt_changes", {}),
                "requested_draws": manifest["requested_draws"], "draws_notice": manifest.get("draws_notice")} if archived_old else {}),
            **({"answers_hashed": True} if salt is not None else {}),
            "answer": manifest.get("answer", "text"),
            "tool_confusion": [{"old_mode": old, "new_mode": new, "count": count}
                               for (old, new), count in sorted(confusion.items(), key=lambda p: str(p[0]))] if tool_mode else [],
            "served_models": served, "n_items": n, "draws": r, "batch": manifest["batch"],
            "labels": labels, "warnings": warnings, "hazard_summary": summary, "hazards": hazards,
            "draw_strip": strips, "self_disagreement_floor": floors, "gold": gold_result,
            "parse_policies": policy_result,
            "unlabelled": {"n_items": len(changed_items), "items": changed_items, "permutations": PERMUTATIONS,
                           "seed": 0, "power": power,
                           "method": "total variation of both answer distributions, pooled 2R reallocation, inclusive +1 Monte Carlo p; BH over all unlabelled items"},
            "review_queue": queue,
            "limitations": (["Tool calls are per conversation; tool mode requires batch 1. Choice accuracy scores the first tool name independently of argument validity.",
                              "Argument checks cover required keys, nested objects/arrays, tuple items, boolean subschemas, patternProperties, additionalProperties and basic JSON types; other JSON Schema constraints are not evaluated.",
                              "Tool hazards count final replies per model per draw. Invalid arguments and unknown names inspect every tool call, including calls after the first. Raw arguments stay in the local run capture.",
                              "The openai and anthropic tool adapters are tested against stubbed responses only."] if tool_mode else []) +
                            (["Batching changes behaviour. Replay production batching and prompt format; compare another batching as a sensitivity check."] if not tool_mode else []) +
                            ["Failures and off-list answers count as wrong with gold. Missing calls are never dropped. Retry failures are shown separately from final outcomes.",
                            "Stable items: old correct at least 80% and new at most 20%, or reverse, using integer thresholds. These are descriptive review candidates.",
                            "Slice min-p treats items as exchangeable. A whole batch failing together can violate that, so flags need review. migration-check does not run a whole-draw sensitivity analysis or a self-null calibration.",
                            "Without gold, CHANGED means modal answers differ and BH-adjusted permutation p <= 0.05. It does not establish which model is better.",
                            "No-gold p uses total variation and 10,000 seed-0 pooled reallocations. Exact weighted enumeration is also shown when within the state budget. Mode ties use a deterministic lexical order, with missing answers first.",
                            "Self-disagreement includes failed answers as a distinct outcome; mean flip rate is the share off each item's own mode. Pairwise floor uses distinct draws.",
                            "One pair at a time. Tool names use exact spelling and case." if tool_mode else
                            "One pair at a time. Classification-style answers work best. Free text is compared after whitespace and case normalization only."]}
