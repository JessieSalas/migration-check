"""Input validation and the numbered-answer wire format used in the original study scripts (not included)."""

import json
from pathlib import Path
import re

SYSTEM = "You are a text classifier. Follow the user's output format exactly."


def read_inputs(path):
    rows, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on input line {number}: {exc.msg}, column {exc.colno}") from exc
        try:
            json.dumps(row, allow_nan=False)
        except ValueError as exc:
            raise ValueError(f"Input line {number}: non-finite numbers are not allowed") from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError(f"Input line {number} needs a nonempty string id")
        if row["id"] in seen:
            raise ValueError(f"Duplicate input id on line {number}")
        seen.add(row["id"])
        has_input, has_messages = "input" in row, "messages" in row
        if has_input == has_messages:
            raise ValueError(f"Input line {number} needs exactly one of input or messages")
        if has_input and (not isinstance(row["input"], str) or not row["input"].strip()):
            raise ValueError(f"Input line {number}: input must be a nonempty string")
        if has_messages:
            messages = row["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"Input line {number}: messages must be a nonempty chat list")
            for message in messages:
                if (not isinstance(message, dict) or message.get("role") not in
                        ("system", "user", "assistant") or not isinstance(message.get("content"), str)):
                    raise ValueError(f"Input line {number}: chat messages need role and string content")
                if not message["content"].strip():
                    raise ValueError(f"Input line {number}: message content must not be empty or whitespace-only")
            if not any(m["role"] == "user" for m in messages):
                raise ValueError(f"Input line {number}: chat needs a user message")
            if [m for m in messages if m["role"] != "system"][-1]["role"] != "user":
                raise ValueError(f"Input line {number}: the last message must be a user turn")
        for field in ("gold", "slice"):
            if field in row and not isinstance(row[field], str):
                raise ValueError(f"Input line {number}: {field} must be a string")
        rows.append(row)
    if not rows:
        raise ValueError("Inputs must contain at least one row")
    return rows


def read_labels(path):
    if path is None:
        return []
    labels = [s.strip() for s in Path(path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
    if not labels or len(set(labels)) != len(labels):
        raise ValueError("Labels must be nonempty and unique")
    return labels


def validate_gold(rows, labels, source="labels.txt"):
    if not labels:
        return
    invalid = []
    for row in rows:
        if "gold" in row and row["gold"] not in labels:
            matches = [label for label in labels if label.casefold() == row["gold"].casefold()]
            hint = f" (case mismatch; use {matches!r})" if matches else ""
            invalid.append(f"{row['id']!r}: {row['gold']!r}{hint}")
    if invalid:
        raise ValueError("Gold values outside " + source + ": " + "; ".join(invalid) +
                         ". Gold and labels must match exact spelling and case.")


def item_text(row):
    return row.get("input", json.dumps(row.get("messages"), ensure_ascii=False))


def build_request(rows, labels, system=SYSTEM, template=None, batched=False):
    """Preserve single-item chat roles; batch chats are numbered JSON transcripts.

    Gold and slice metadata never enter a request.
    """
    label_hint = "\nAllowed answers (exact spelling):\n" + ", ".join(labels) if labels else ""
    if batched:
        texts = [template.replace("{input}", item_text(row)) if template else item_text(row) for row in rows]
        lines = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(texts))
        prompt = ("Assign each message exactly one answer." + label_hint + f"\n\nMessages:\n{lines}\n\n"
                  "Reply with only a JSON object that maps each message number to its "
                  f"answer, for all {len(rows)} messages, like {{\"1\": \"<answer>\", \"2\": \"<answer>\"}}. No other text.")
        return system, [{"role": "user", "content": prompt}]
    row = rows[0]
    if "messages" in row and template is None:
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in row["messages"] if m["role"] != "system"]
        system = "\n\n".join([system] + [m["content"] for m in row["messages"] if m["role"] == "system"])
    else:
        text = item_text(row)
        messages = [{"role": "user", "content": template.replace("{input}", text) if template else text}]
    if labels:
        messages.append({"role": "user", "content": label_hint + "\nReply with only the answer. No other text."})
    return system, messages


def keyed_answers(obj, size):
    """Per-item answers from one keyed object: message numbers "1".."size"."""
    # Nulls, containers and empty strings are missing answers, never stringified labels.
    return [obj.get(str(i + 1)).strip() if isinstance(obj.get(str(i + 1)), str)
            and obj[str(i + 1)].strip() else None for i in range(size)]


def parse_keyed(raw, size):
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return [None] * size
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, RecursionError):
        return [None] * size
    if not isinstance(obj, dict):
        return [None] * size
    return keyed_answers(obj, size)


# Characters that can change nesting or string state; everything else is skipped.
STRUCTURE = re.compile(r'[{}\[\]"\\]')
# An opener that never closes is prose: rescan after it, a bounded number of times.
MAX_RESCANS = 4


def top_level_spans(raw):
    """(start, end) of each balanced {...} span at nesting depth 0 of a reply.

    One pass over the structural characters tracks [ ] and { } depth and string
    state, so objects inside an array or inside a malformed outer object are never
    top level. Quotes open strings only inside a structure, so prose quotes cannot
    hide an object. Stray closers are ignored. Cost is linear in the reply length
    for each of at most MAX_RESCANS + 1 passes.
    """
    spans, position = [], 0
    for _ in range(MAX_RESCANS + 1):
        stack, in_string, skip = [], False, -1
        for match in STRUCTURE.finditer(raw, position):
            index, char = match.start(), match.group()
            if index < skip:
                continue
            if in_string:
                if char == "\\":
                    skip = index + 2
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = bool(stack)
            elif char in "{[":
                stack.append((char, index))
            elif char in "}]" and stack and stack[-1][0] == ("{" if char == "}" else "["):
                opener, start = stack.pop()
                if not stack and opener == "{":
                    spans.append((start, index + 1))
        if not stack:
            break
        position = stack[0][1] + 1
    return spans


def json_objects(raw):
    """Top-level JSON objects in a reply, in order.

    Only balanced {...} spans at depth 0 are decoded, so nested objects, objects in
    arrays and fragments of a malformed object are never counted. Text between
    objects is skipped.
    """
    objects = []
    for start, end in top_level_spans(raw):
        try:
            obj = json.loads(raw[start:end])
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def answer_objects(raw, size):
    """Top-level objects holding at least one message-number key "1".."size"."""
    keys = {str(i + 1) for i in range(size)}
    return [obj for obj in json_objects(raw) if keys & obj.keys()]


def recover_keyed(raw, size):
    """(n_answer_objects, recovered) for a batched reply.

    Only answer objects count, so a format example such as {"key": "label"} or an
    empty {} in prose is not a second answer. recovered merges them in order: a
    later nonempty string answer replaces an earlier one, while a later null, blank
    or container never erases an answer. This is descriptive: parse_keyed still
    treats a multi-object reply as a failure.
    """
    objects, merged = answer_objects(raw, size), {}
    for obj in objects:
        merged.update({key: value for key, value in obj.items() if isinstance(value, str) and value.strip()})
    return len(objects), keyed_answers(merged, size)


def parse_answer(raw, size, batched):
    if batched:
        return parse_keyed(raw, size)
    return [raw.strip() or None]


def pattern(answer, labels):
    if answer is None:
        return "parse_failure"
    if not labels or answer in labels:
        return None
    if answer.replace("_", " ") in labels:
        return "snake_case"
    if answer.casefold() in {label.casefold() for label in labels}:
        return "case_change"
    if any(label in answer for label in labels):
        return "extra_text"
    return "other_off_list"


def normalize(answer, labels):
    # Classification spelling remains strict, so snake_case stays a hazard.
    return answer if answer is None or labels else " ".join(answer.split()).casefold()
