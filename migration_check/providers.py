"""Small stdlib adapters. No SDKs, telemetry, redirects, or secondary endpoints."""

import hashlib
import json
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .tools import REPLY_FAILURES, mock_tool

# Persistence is deliberately disabled in addition to the reference isolation flags.
CLAUDE_FLAGS = ["--output-format", "json", "--tools", "", "--setting-sources", "",
                "--strict-mcp-config", "--no-session-persistence"]
PROVIDERS = ("anthropic", "openai", "openai-compatible", "claude-cli", "mock")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Claude Code's documented switch that turns off its own nonessential traffic
# (telemetry, error reporting, update checks). Set for every claude-cli call.
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def program_name():
    """How the user invoked us, so messages give a command that works for them."""
    entry = Path(sys.argv[0] if sys.argv and sys.argv[0] else "migration-check")
    return ("python3 -m migration_check" if (entry.name, entry.parent.name) == ("__main__.py", "migration_check")
            else "migration-check")


def claude_missing():
    prog = program_name()
    return ("claude CLI not found on PATH. The claude-cli provider runs Anthropic's Claude Code CLI.\n"
            "  - Install Claude Code (for example `npm install -g @anthropic-ai/claude-code`; see Anthropic's "
            "Claude Code setup docs), run `claude` once and log in with /login, then rerun this command; or\n"
            f"  - run `{prog} sample --offline` to see a simulated report in about a second; or\n"
            "  - use another provider, such as anthropic:MODEL or openai:MODEL with your own key.\n"
            "    Those HTTP adapters are tested against stubbed responses only and are not yet live-tested.")


class ProviderError(RuntimeError):
    """A provider failure whose message has been made safe for local summaries."""


def safe_tool_error(message):
    """Tool errors can echo partial or reformatted definitions and arguments.

    Keep only known status codes/categories rather than trying to redact values.
    """
    text = str(message)
    status = re.match(r"provider HTTP ([0-9]{3})(?:\b|$)", text)
    if status:
        return "provider HTTP " + status[1]
    if text in REPLY_FAILURES or text in (
            "provider unreachable: connection failed", "provider unreachable: connection timed out",
            "provider unreachable: connection refused", "provider response exceeds 4 MiB limit"):
        return text
    return "provider error (details hidden)"


def safe_status(message, sensitive=()):
    text = str(message)
    secrets = [v for k, v in os.environ.items() if v and
               (k.endswith("_KEY") or k.endswith("_TOKEN"))]
    for value in sorted(set(secrets + list(sensitive)), key=len, reverse=True):
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+|\bsk-[\w-]+", "[redacted]", text)
    return " ".join(text.split())[:200]


def model_ids(values):
    if not isinstance(values, (list, tuple, dict)):
        return []
    return sorted({value[:200] for value in values if isinstance(value, str) and value})


def split_model(spec):
    provider, sep, model = spec.partition(":")
    if not sep or provider not in PROVIDERS or not model.strip():
        raise ValueError("Model must be <provider>:<model>; providers: " + ", ".join(PROVIDERS))
    return provider, model


def preflight(spec):
    """Checks that need no network: run before any output file is written."""
    provider, _ = split_model(spec)
    if provider == "claude-cli" and shutil.which("claude") is None:
        raise ValueError(claude_missing())
    variable = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "openai-compatible": "MIGRATION_CHECK_BASE_URL"}.get(provider)
    if variable and not os.environ.get(variable):
        raise ValueError(f"Set {variable} before running {provider}")
    if provider == "openai-compatible":
        url = urllib.parse.urlsplit(os.environ[variable])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("MIGRATION_CHECK_BASE_URL must be an http(s) API base URL without credentials or query")
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            loopback = url.hostname.lower() == "localhost"
        if url.scheme == "http" and os.environ.get("MIGRATION_CHECK_API_KEY") and not loopback:
            raise ValueError("Refusing to send MIGRATION_CHECK_API_KEY over HTTP to a non-loopback host; use HTTPS")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Some API hosts (Together AI among them, verified 1 Nov 2026) answer HTTP 403 to urllib's default
# "Python-urllib/x.y" agent, so every request names this tool instead.
def _user_agent():
    from . import __version__
    return f"migration-check/{__version__}"


USER_AGENT = _user_agent()


def post(url, payload, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
                                     method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ProviderError("provider response exceeds 4 MiB limit")
            return json.loads(body.decode("utf-8-sig"))
    except urllib.error.HTTPError as exc:
        if payload.get("tools"):
            raise ProviderError(f"provider HTTP {exc.code}") from None
        # Only extract the short error message, never an arbitrary response body.
        detail = ""
        try:
            body = json.loads(exc.read(8192).decode("utf-8-sig"))
            error = body.get("error", {}) if isinstance(body, dict) else {}
            detail = error.get("message", "") if isinstance(error, dict) else ""
        except (ValueError, OSError):
            pass
        messages = payload.get("messages", [])
        sensitive = [m.get("content", "") for m in messages if isinstance(m.get("content"), str)]
        sensitive += [payload["system"]] if isinstance(payload.get("system"), str) else []
        sensitive += [value.removeprefix("Bearer ") for name, value in headers.items()
                      if name.lower() in ("authorization", "x-api-key")]
        detail = safe_status(detail, sensitive) if isinstance(detail, str) else ""
        raise ProviderError(f"provider HTTP {exc.code}" + (": " + detail if detail else "")) from None
    except urllib.error.URLError as exc:
        reason = "connection failed"
        if isinstance(exc.reason, TimeoutError):
            reason = "connection timed out"
        elif isinstance(exc.reason, ConnectionRefusedError):
            reason = "connection refused"
        raise ProviderError("provider unreachable: " + reason) from None


def call(spec, system, messages, context, timeout=240):
    provider, model = split_model(spec)
    tools = context.get("tools")
    if context.get("answer") == "tool" and provider == "claude-cli":
        raise ValueError("claude-cli does not support tool mode; use anthropic, openai, openai-compatible or mock")
    if provider == "mock":
        return mock(model, context)
    if provider == "claude-cli":
        flags = list(CLAUDE_FLAGS)
        # A fresh empty directory outside the project prevents repository instructions being loaded.
        with tempfile.TemporaryDirectory(prefix="migration-check-", dir=tempfile.gettempdir()) as directory:
            system_path = Path(directory) / "system.txt"
            fd = os.open(system_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(system)
            flags += ["--system-prompt-file", str(system_path)]
            prompt = (messages[0]["content"] if len(messages) == 1 else
                      "Continue this conversation:\n" + json.dumps(messages, ensure_ascii=False))
            result = subprocess.run(["claude", "-p", "--model", model] + flags, input=prompt,
                                    capture_output=True, text=True, cwd=directory, timeout=timeout,
                                    env={**os.environ, **CLAUDE_ENV})
        try:
            response = json.loads(result.stdout)
        except ValueError:
            raise ProviderError(f"claude-cli invalid envelope (exit {result.returncode}); verify CLI support for "
                                "--system-prompt-file and --no-session-persistence") from None
        usage = response.get("modelUsage")
        return {"text": response.get("result") or "", "served_models": model_ids(usage) if isinstance(usage, dict) else [],
                "error": ("claude-cli error (exit " + str(result.returncode) + "): " +
                          safe_status(response.get("result", ""), [system] + [m["content"] for m in messages]))
                         if result.returncode or response.get("is_error") else None,
                "usage": response.get("modelUsage") or {}}
    if provider == "anthropic":
        payload = {"model": model, "system": system, "messages": messages, "max_tokens": 4096}
        if tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"]["description"],
                                 "input_schema": t["function"]["parameters"]} for t in tools]
        response = post("https://api.anthropic.com/v1/messages",
                        payload,
                        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}, timeout)
        text = "".join(block.get("text", "") for block in response.get("content", []) if block.get("type") == "text")
        tool_calls = [{"name": block.get("name"), "arguments": json.dumps(block.get("input"))}
                      for block in response.get("content", []) if block.get("type") == "tool_use"]
        stops = {"stop_reason": response.get("stop_reason")}
    else:
        preflight(spec)
        base = "https://api.openai.com/v1" if provider == "openai" else os.environ["MIGRATION_CHECK_BASE_URL"].rstrip("/")
        key = os.environ.get("OPENAI_API_KEY" if provider == "openai" else "MIGRATION_CHECK_API_KEY", "")
        payload = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        response = post(base + "/chat/completions", payload,
                        {"Authorization": "Bearer " + key} if key else {}, timeout)
        choice = response["choices"][0]
        message = choice["message"]
        stops = {"finish_reason": choice.get("finish_reason"), "refusal": message.get("refusal")}
        text = message.get("content") or ""
        tool_calls = [{"name": c.get("function", {}).get("name"),
                       "arguments": c.get("function", {}).get("arguments")}
                      for c in message.get("tool_calls") or []]
    return {"text": text, "served_models": model_ids([response.get("model")]),
            "error": None, "usage": response.get("usage", {}), "tool_calls": tool_calls, **stops}


SAMPLE_MOCKS = ("sample-old", "sample-new")
SAMPLE_UNPARSEABLE = "I could not give one label for each of these messages."


def _unit(*parts):
    return int.from_bytes(hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).digest()[:8], "big")


def _stems(text):
    return {word[:5] for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 2}


def sample_mock(model, context):
    """Offline stand-in used by `sample --offline`. It is a keyword matcher, not a model.

    Each answer is the label sharing the most word stems with the input (a stable
    pseudo-random label when nothing matches). Both arms add 5% run-to-run noise.
    sample-new also changes 10% of items consistently. Format failures copy the
    study's shape: one whole old-arm batch comes back upper-cased in draw 2, and
    new-arm batch 2 returns an unparseable reply in two of every three draws.
    Gold is never available to it.
    """
    labels = context["labels"] or ["yes", "no"]
    draw, batch = context["draw"], context["batch"]
    if model == "sample-new" and context["batched"] and batch == 1 and draw % 3 != 2:
        return {"text": SAMPLE_UNPARSEABLE, "served_models": [model], "error": None, "usage": {}}
    answers = []
    for item_id, text in zip(context["item_ids"], context["inputs"]):
        words = _stems(text)
        scores = [len(_stems(label.replace("_", " ")) & words) for label in labels]
        index = scores.index(max(scores)) if max(scores) else _unit("base", item_id) % len(labels)
        if len(labels) > 1:
            if model == "sample-new" and _unit("changed", item_id) % 100 < 10:
                index = (index + 1) % len(labels)
            if _unit("noise", model, item_id, draw) % 100 < 5:
                index = (index + 1 + _unit("shift", model, item_id, draw) % (len(labels) - 1)) % len(labels)
        answers.append(labels[index])
    if model == "sample-old" and draw == 1 and batch == 2:
        answers = [answer.upper() for answer in answers]
    text = json.dumps({str(i + 1): answer for i, answer in enumerate(answers)}) if context["batched"] else answers[0]
    return {"text": text, "served_models": [model], "error": None, "usage": {}}


def mock(model, context):
    """Deterministic fixtures; never inspect gold. Modes are documented in README."""
    if context.get("answer") == "tool":
        return {**mock_tool(model, context), "served_models": [model], "error": None, "usage": {}}
    if model in SAMPLE_MOCKS:
        return sample_mock(model, context)
    labels = context["labels"] or ["yes", "no"]
    answer = labels[1 % len(labels)] if model in ("new", "snake", "case", "extra") else labels[0]
    if model == "flaky" and context["draw"] % 2:
        answer = labels[1 % len(labels)]
    if model == "snake":
        answer = answer.replace(" ", "_")
    if model == "case":
        answer = answer.upper()
    if model == "extra":
        answer = "The answer is " + answer + "."
    answers = {str(i + 1): answer for i in range(context["size"])}
    if model == "partial":
        answers.pop("1", None)
    text = json.dumps(answers) if context["batched"] else answer
    if model == "self-correct" and context["batched"]:
        # The model answers, writes "Wait", then a second JSON object correcting the
        # last message to the second label: in full on even draws, only that key on odd.
        last, fixed = str(context["size"]), labels[1 % len(labels)]
        correction = {**answers, last: fixed} if context["draw"] % 2 == 0 else {last: fixed}
        text += f"\n\nWait, message {last} is {fixed}.\n\n" + json.dumps(correction)
    if model == "fail" or (model == "retry" and context["attempt"] == 0):
        return {"text": "MOCK_RAW_FAILURE_PRIVATE", "served_models": [model], "error": "mock failure", "usage": {}}
    return {"text": text, "served_models": ["unexpected-model" if model == "mismatch" else model],
            "error": None, "usage": {}}
