# migration-check

Compare an old and a new model on your own inputs, many times over, before you switch. Python 3.9+, standard library only, MIT licence (the bundled Banking77 sample is CC BY 4.0; see its NOTICE).

**Sonnet 4.5 (`claude-sonnet-4-5-20250929`) retires from the Claude API on 30 Nov 2026 ([Anthropic's model deprecations page](https://docs.claude.com/en/docs/about-claude/model-deprecations)). Before then: run migration-check on 20 of your own inputs, star the repository if it helps, and open an issue with what you find ([what to share](#what-to-share-in-an-issue)).**

## Why

- The same model disagrees with itself: over the same 620 inputs, ten runs of Sonnet 4.5 ranged from 83.7% to 87.3% accuracy, mostly because a whole batch of 20 answers sometimes came back in a different label format.
- Batching can change behaviour: in one batch of 20, Sonnet 5.5 returned an unparseable reply in 8 of 10 runs; sent one at a time, the same 20 items never failed (0 of 40 calls).
- So a one-shot old-vs-new comparison mixes real changes with format failures and run-to-run noise.

These numbers come from our study of one model pair on samples of two public intent datasets (HWU64 and CLINC150), plus an exploratory run on Banking77 that the live sample's intents are drawn from, sent through the claude CLI at its default settings. Run records and methods are in [TESTING.md](TESTING.md).

## Quickstart

### 1. Install

With pipx:

```sh
pipx install git+https://github.com/JessieSalas/migration-check
```

Or with pip, in a virtual environment:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install git+https://github.com/JessieSalas/migration-check
```

Or with no install at all: clone it and work from the clone's root, typing `python3 -m migration_check` wherever this README says `migration-check` (it has no dependencies).

```sh
git clone https://github.com/JessieSalas/migration-check && cd migration-check
python3 -m migration_check --version
```

Python 3.9 or newer is needed. The test suite passes on the `python3` that ships with macOS (3.9.6).

### 2. See the report now (no account, about a second)

```sh
migration-check sample --offline
```

This runs the bundled sample (100 synthetic support tickets, 10 intent labels) against two offline mock providers. They are keyword matchers, not models. It writes `./migration-check-sample-offline/` with `report.html`, `report.decision.json`, `report.decision.md` and `report.analysis.json`, including format failures and queued changes so you can see every section. The report header, the decision record and the terminal output all say SIMULATED; its numbers are not evidence about any model.

### 3. Run it live (needs Claude Code installed and logged in)

```sh
migration-check sample
```

This runs Sonnet 4.5 against Sonnet 5.5 through your local `claude` CLI on a different bundled sample: 100 public Banking77 support tickets (10 for each of 10 intents, scored against all 77 Banking77 labels; CC BY 4.0, see `migration_check/sample/banking77/NOTICE`). The 10 intents were picked from the exploratory Banking77 run in our study to include some that moved and some that did not, so the sample shows movement by design; `migration_check/sample/banking77/README.md` lists them. It makes 30 calls before retries (3 draws per model, 5 batches of 20; a failed call is tried up to 3 times) and took 52 to 112 seconds in the maintainer's three live runs on 1 Nov 2026 (57 seconds from a fresh clone to a written report on v0.4.1; see TESTING.md). The calls count against your Claude plan usage, or your API billing if Claude Code is set up with an API key.

It writes `./migration-check-sample/` with the run capture and the same four report files, then prints the elapsed time, the report's `file://` URL, the decision record paths and what to look at first. The sample is public data, so its report shows each queued ticket's text and gold label; reports on your own runs hide input text unless you pass `--show-text`.

- Three draws keep the sample quick; a real audit should use 10 (`--draws 10`). `--old`, `--new`, `--draws` and `--out` override the defaults.
- If the output directory is not empty, the run goes into a new timestamped subdirectory, so an earlier run and its reviewer edits are never overwritten.
- If `claude` is not on your PATH, the command stops before writing anything and lists your options. If `claude` is not logged in, the first batch fails, nothing is kept, and the message tells you to run `claude` and log in with `/login`; then rerun the same command.
- Through the claude CLI each model runs with Claude Code's default thinking setting. In our runs Sonnet 4.5 used extended thinking and Sonnet 5.5 did not, so differences are between the two models as configured, not bare models.

## What to share in an issue

Share:

- the migration-check version (`migration-check --version`) and your `--old` and `--new` values
- the served model IDs ("Served:" on each model card in the report, or `served_models` in `report.analysis.json`)
- draws and batch size
- the "Look first" line the command printed
- the format-hazard line, the parse-policy table, any Self-correction line and the strict accuracy (D\*) line near the top of `report.decision.md`

Never share `inputs.jsonl`, `calls.jsonl`, `privacy-salt`, a report built with `--show-text` (the bundled sample's reports are fine: their text is public or synthetic), or `manifest.json` unless your system prompt and labels are fine to publish. Even without `--show-text`, reports and decision records list your item IDs, slice names, gold labels and label list.

## Run it on your own inputs

```sh
migration-check run --inputs my_inputs.jsonl --labels my_labels.txt \
  --old claude-cli:claude-sonnet-4-5-20250929 --new claude-cli:claude-sonnet-5-5 \
  --draws 10 --batch 20 --out my-audit/runs
migration-check report --runs my-audit/runs --out my-audit/report.html
```

Inputs are JSONL, one object per line, with a unique string `id` and either a string `input` or a `messages` chat list. `gold` (the expected label) and `slice` (a group name for per-group results) are optional and are never sent to a model:

```json
{"id": "t-1", "input": "My new card was due last week and still has not come.", "gold": "card_not_arrived"}
{"id": "t-2", "input": "Please shut my account down for good.", "gold": "close_account", "slice": "closures"}
```

`my_labels.txt` lists the allowed answers, one per line, exactly as you want them spelled. Every `gold` value must be one of them. `migration_check/sample/tickets.jsonl` and `migration_check/sample/labels.txt` in this repository are complete examples of both files.

Unless you pass `--system`, every call uses this system prompt: "You are a text classifier. Follow the user's output format exactly." The label list and fixed instructions on the answer format are added to each request.

Use your production batch size. Batching can change behaviour: in our runs a batch failed where the same items sent alone did not. Replay how you call the model in production (batch size, system prompt via `--system`, user template via `--template`), then try another batch size as a sensitivity check.

Cost: each model gets `draws x ceil(inputs / batch)` calls before retries. 20 inputs at batch 20 and 10 draws is 10 calls per model. Replies that fail to parse, self-corrected ones included, are retried, so a model with format hazards can use up to (retries + 1) times as many calls. `claude-cli` calls use your Claude plan usage or API billing, whichever Claude Code is set up with; the HTTP adapters bill the API key you export.

## What it tells you

1. **Format failures first.** Missing or unparseable replies and off-list labels (snake_case, case changes, extra text), per model, per draw and per batch. In our one-pair study they dominated run-to-run variance and could move a whole run by about 3 points, in either direction.
2. **Changes beyond each model's own run-to-run noise.** Each model answers every input several times, so the report shows how much each disagrees with itself before it counts a difference between them. With gold: per-draw accuracy, the accuracy difference, per-slice tests, and items that are stably right on one model and stably wrong on the other. Without gold: items whose answer distribution changed beyond noise (permutation test with BH correction).
3. **A review queue** of the items worth a human look, with both models' answer counts.
4. **A decision record** (`.decision.json` and `.decision.md`) you fill in with a decision, reviewer and notes, in the HTML page or by editing the JSON, then refresh by rerunning `report`. It opens with a one-paragraph decision summary generated from the numbers (also at the top of the HTML page); the summary states evidence and makes no decision.

**Self-correction** is one format hazard named on its own. Asked for one JSON object for a batch, a model can answer, write a short note such as "Wait, message 2 is ...", then write a second, corrected JSON object in the same reply. A parser expecting one object fails on every such reply, so migration-check still counts it as a parse failure. The Format hazards section names it with the number of calls, and a table puts each model's numbers side by side under separately named parse policies, each with its denominator: strict parse success (calls and answers), strict accuracy over all answers (a failure counts wrong), accuracy among parsed answers only, strict accuracy without retries, and accuracy under last-object recovery and under object-merge recovery (later answers win). The recovery policies only re-read the stored replies; the run's own answers stay strict. In one study harness (Claude Sonnet 5.5 through the Claude CLI at its defaults, one 6-item batch, 20 calls), 17 of the 20 replies had this shape; merging recovered all 102 of their answers, all correct, and the correction was right every time (17 of 17). Across all 20 replies, 118 of 120 answers were correct after merging; the 2 misses were in the 3 single-object replies, which parsed normally. `mock:self-correct` reproduces the shape offline.

Worked example, with this tool: we ran `migration-check run` on those 6 messages, all 150 CLINC150 labels, `--batch 6 --draws 10`, Sonnet 4.5 against Sonnet 5.5 through the Claude CLI. The messages are rows 1772, 1901, 1338, 2414, 3165 and 1503 (in that order) of the test split in CLINC150's `data_full.json` (public, CC BY 3.0; not bundled here). migration-check's own prompt reproduced the shape: 29 of 30 Sonnet 5.5 attempts failed to parse, and every one of the 10 calls showed it. Nine calls still ended unparseable after 3 attempts; the tenth parsed on its third attempt, and its 6 answers (5 correct) are the only scored Sonnet 5.5 answers, so 54 of 60 final answers were unparseable and Sonnet 5.5 scored 5 of 60. In those 9 calls, merging the objects recovered 54 of 54 answers, all correct. Sonnet 4.5 had no format hazards and scored 60 of 60. Almost all of the headline gap (100.0% against 8.3%) is a format hazard: 54 of the 55 answers Sonnet 5.5 missed were in replies the strict parser could not read, and merging the objects recovers all 54 correctly. The one remaining miss is the sixth message in the single call that answered once without correcting itself. Every self-correcting reply also got that message wrong in its first object before correcting it. That is exactly why the report lists format hazards first. The 6 messages themselves were found by a separate reduction script, not by migration-check, and at the CLI's defaults Sonnet 4.5 thinks before answering while Sonnet 5.5 does not, so this compares the two as configured. In the study, raising Sonnet 5.5's effort to high still produced the shape in 11 of 20 calls, so the thinking setting is not the whole story.

It never says a migration is safe. The most it says is "no regression detected across the tested inputs and draws", and only when strict accuracy on gold does not drop beyond its 95% interval, the new model has no missing or off-list answers, no input without gold changed beyond noise, and the review queue is empty. An empty review queue does not establish suitability either.

**Off-list snake_case** is a known hazard class too: a model returns a label with underscores for spaces, such as `general_quirky` when the list says `general quirky`. migration-check reports it as an off-list answer with the pattern `snake_case` and counts it wrong. It does not silently normalize it to the label, because a model that changes how it spells labels has changed its output contract, and an exact-match parser in production would break the same way. Normalizing would hide that change.

Because off-list answers parse, "accuracy among parsed answers" cannot separate a spelling change from wrong answers, so the parse-policy table also has **accuracy among on-list answers only**: correct answers over answers that parsed and are spelled as an allowed label, per model, with denominators. A **where the hazards are** line says how many calls hold each model's off-list and missing answers (for example, 220 off-list answers, all in 11 of 155 calls, every answer in each of those calls), which separates a whole batch that switched spelling from scattered mistakes. The decision summary says the drop is "an output-format change, not a loss of task accuracy on the answers that follow the format" only when the new model's on-list accuracy is at or above the old model's. When it is lower but the item-clustered 95% interval of the difference includes zero, it says no loss was detected and names the largest loss the interval does not rule out; otherwise it says both changed. On-list accuracy is descriptive, since the inputs a model answers off-list drop out of its denominator. Strict accuracy stays authoritative while the output contract is unchanged.

Off-list examples (up to 3 per model, most frequent first, each beside its closest allowed label, such as `"datetime_query"` beside allowed `"datetime query"`) appear only in reports built with `--show-text`. The label it is shown beside comes from your label list, but the off-list answer is model output, which can contain anything, input text included, so it follows the same default as every other non-label answer: hidden, with a per-run pseudonym where an answer must be shown.

## What it found on its first live runs

Two findings, both from 1 Nov 2026. First, a self-correction hazard in Claude Sonnet 5.5, called through the Claude Code CLI at its defaults (no extended thinking), on a 6-message trigger set from CLINC150's public test split: the model answers the batch, writes a note such as "Wait, message 6 is ...", then writes a second, corrected JSON object, so a strict one-object parser reads no answer from the reply (the worked example above). Scope: the CLI wraps its own system prompt around the request, so this is the tested configuration, not every Sonnet 5.5 deployment. Second, the first live run of the `openai-compatible` adapter, against Together AI, met HTTP 403 because Together rejects urllib's default `Python-urllib` User-Agent; since v0.4.2 every request sends `User-Agent: migration-check/<version>`, which Together accepts. Details are in [TESTING.md](TESTING.md).

The same day it compared three real upgrades on Together AI across three public datasets, with three different outcomes: one cleared, one with intents worse behind a flat average, and one cheaper tier that broke the label format on one task while improving another. Those results, with one rerun command per claim and its cost, are in [docs/FINDINGS-2026-11-01.md](docs/FINDINGS-2026-11-01.md); the 6-message reproducer is in [examples/sonnet55-self-correction](examples/sonnet55-self-correction/README.md).

## Scope, honestly

**What it scores:** labels, and the first tool choice. Nothing else.

- **Labels** (`--labels`): an answer is correct only if, after trimming surrounding whitespace, it equals the gold label exactly, spelling and case included. A different label from the list is simply wrong. An answer not on the list is off-list, sorted into snake_case, case change, extra text or other, and counted as wrong.
- **First tool choice** (`--answer tool --tools tools.json`): the name of the first tool called, or `none` for a text reply. Arguments are checked structurally (required keys, basic types and a few schema keywords) and reported as hazards, not scored. Tool mode runs one conversation per call. It is available only through the HTTP adapters (`anthropic:`, `openai:`, `openai-compatible:`), not `claude-cli`, and those adapters are tested against stubbed responses only.
- **Free text** (no `--labels`): scored only by exact match after collapsing whitespace and ignoring case. With gold, an answer is correct only if it equals the gold text after that normalisation; without gold, the per-item answer distributions are compared. There is no semantic, fuzzy, embedding or model-graded comparison, so "Paris" and "Paris, France" are different answers. This only makes sense for short closed-form answers such as an ID, a number or a single word. In reports, free-text answers appear as per-run pseudonyms (HMAC) unless you pass `--show-text`.

One old/new pair at a time. It is not a general eval framework; for eval suites, CI gates and LLM-judge rubrics use a tool built for that, such as Promptfoo. migration-check can sit beside one.

### Providers

| Provider          | Spec                      | Sends requests to                                                                                                        | Credentials                                                                                                                   | Tested                                                          |
| ----------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Claude CLI        | `claude-cli:MODEL`        | your local `claude` CLI (Claude Code), which calls Anthropic, or AWS Bedrock or Google Vertex if your shell selects them | whatever Claude Code uses in print mode: your login, or `ANTHROPIC_API_KEY` or another provider variable if set in your shell | unit-tested with a stubbed subprocess; live-tested by the maintainer on 1 Nov 2026 (TESTING.md) |
| Mock              | `mock:MODE`               | nowhere; answers are generated locally                                                                                   | none                                                                                                                          | unit-tested                                                     |
| Anthropic         | `anthropic:MODEL`         | `https://api.anthropic.com/v1/messages`                                                                                  | `ANTHROPIC_API_KEY`                                                                                                           | tested against stubbed HTTP responses only; not yet live-tested |
| OpenAI            | `openai:MODEL`            | `https://api.openai.com/v1/chat/completions`                                                                             | `OPENAI_API_KEY`                                                                                                              | tested against stubbed HTTP responses only; not yet live-tested |
| OpenAI-compatible | `openai-compatible:MODEL` | `$MIGRATION_CHECK_BASE_URL/chat/completions`                                                                             | `MIGRATION_CHECK_API_KEY` (optional)                                                                                          | tested against stubbed HTTP responses; live-tested against Together AI on 1 Nov 2026 (TESTING.md) |

`claude-cli` does not support tool mode.

### No telemetry

migration-check itself sends nothing: it makes no network request except the provider calls you ask for, and has no analytics, update checks or crash reports.

The `claude` CLI is Anthropic's own program and may send its own usage and error reports to Anthropic. migration-check runs it with `--setting-sources ''`, so your Claude Code `settings.json` is not loaded, and sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` in its environment, Claude Code's documented switch for turning off its nonessential traffic.

### What is sent where

- Your inputs (the `input` text or `messages`), the system prompt, the label list, the fixed answer-format instructions and the model ID go only to the provider you choose, with your credentials. `gold` and `slice` are never sent.
- `claude-cli`: the prompt goes to `claude -p` on stdin. The system prompt is written to a 0600 file in a fresh `migration-check-*` temporary directory, passed with `--system-prompt-file` and removed after the call. Process arguments carry only the model ID, flags and that file path. The CLI inherits your shell environment, so any `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` there decides which account and service it uses.
- The HTTP adapters honour the standard proxy variables (`HTTPS_PROXY` and similar) through Python's `urllib`.
- Everything else stays on disk in the output directory: `manifest.json` (model IDs, settings, labels, system prompt and template), `inputs.jsonl`, every attempt's raw reply in `calls.jsonl`, and a private `privacy-salt` file. Delete the directory when you are done.
- Reports (`report.html`, `.analysis.json`, decision records) exclude input text and raw replies unless you build them with `report --show-text`. They do include item IDs, slice names, gold labels and the label list, so do not put customer identifiers in `id` if you will share a report. Off-list answers, including the off-list examples, appear as text only with `--show-text`.
- Archives exclude raw reply text unless you create them with `archive --include-raw`. Tool-call arguments are never exported.

## Before your model retires: archive it

Freeze the old model's answers while you can still call it, then compare future models against the frozen copy.

```sh
migration-check archive --runs my-audit/runs --arm old --out incumbent-archive/
migration-check archive-verify incumbent-archive/ --expect-head HEAD
migration-check run --inputs my_inputs.jsonl --labels my_labels.txt \
  --old archive:incumbent-archive/@HEAD --new claude-cli:NEW_MODEL --batch 20 --out replay/runs
```

- Record the printed head hash outside the archive and substitute it for `HEAD`. The hash chain detects accidental edits; deliberate edits are detected only against a head recorded elsewhere.
- Raw text is excluded unless `archive --include-raw`. Keep `privacy-salt` private and with the archive.
- Replay verifies the chain and the inputs hash before any call, then calls only the new arm. It uses the archive's draw count and requires the same batch size, labels, answer mode and tools.
- A different system prompt or template is refused unless you pass `--allow-prompt-change`, which records the difference and warns at the top of reports.
- Reports and decision records mark the old arm ARCHIVED with its head, creation time and original served model IDs.

## Reference

### Provider setup

```sh
export ANTHROPIC_API_KEY='your-key'
migration-check run --inputs my_inputs.jsonl --old anthropic:OLD_MODEL --new anthropic:NEW_MODEL --out runs/anthropic
export OPENAI_API_KEY='your-key'
migration-check run --inputs my_inputs.jsonl --old openai:OLD_MODEL --new openai:NEW_MODEL --out runs/openai
export MIGRATION_CHECK_BASE_URL='http://localhost:8000/v1'
migration-check run --inputs my_inputs.jsonl --old openai-compatible:OLD_MODEL --new openai-compatible:NEW_MODEL --out runs/compatible
```

- OpenAI-compatible uses only `MIGRATION_CHECK_API_KEY`, if set, and never reuses `OPENAI_API_KEY`. Keys require HTTPS except for loopback hosts (`localhost`, `127.0.0.1`, `::1`); leave the key unset for an unauthenticated local server.
- The Claude CLI adapter runs `claude -p` in a fresh temporary directory holding only the system-prompt file, with `--output-format json --tools '' --setting-sources '' --strict-mcp-config --no-session-persistence`, so no project instructions, tools or MCP servers load and no transcript is kept. Older CLI versions without `--system-prompt-file` fail with a clear error.
- Served model IDs come from the response `model` field or the CLI's `modelUsage`; missing IDs and mismatches with the requested ID are warnings in the report.
- HTTP adapters follow the OpenAI Chat Completions and Anthropic Messages request formats. Redirects are refused and responses over 4 MiB are rejected.

### Inputs and run options

- Input and message text must be non-empty. In a chat, the last non-system message must be a user turn. Single-item chat requests keep roles; batched chats are sent as JSON transcripts.
- UTF-8 with or without a BOM, and CRLF line endings, are accepted for inputs and labels.
- `--system system.txt` sets the system prompt. `--template user.txt` substitutes the literal `{input}`.
- Defaults: `--draws 10 --batch 1 --workers 4 --retries 2 --timeout 240`. Batches larger than 1 ask for a JSON object keyed by item number, including the final short batch.
- `--retries 2` allows three attempts for a fully failed call. A partial keyed reply is final; its missing keys are failures. Refusals, truncations and content-filter stops are final too.
- Every attempt keeps latency, raw text, parsed answers, usage, served IDs, stop or finish reason and a short, credential-free error in `calls.jsonl`.
- Missing keys, a missing `claude` CLI and a bad base URL are caught before any file is written. The first batch of each arm is then checked before the rest is scheduled; an arm whose every attempt failed at the provider stops the run early. If nothing was answered, rerunning the same command reuses the output directory and moves the failed attempts into a `failed-start-*` subdirectory.
- Ctrl-C cancels pending calls, saves in-flight ones and exits 130. The partial run can still be reported.
- Reusing an output directory that holds answers is refused, to preserve reviewer work. Rebuilding a report keeps reviewer fields from the existing `.decision.json` of the same audit. Browser downloads include the report name and audit ID; to restore browser edits on rebuild, save the downloaded JSON beside the HTML as `<report>.decision.json`.

### Tool-choice mode

```sh
migration-check run --inputs examples/tool_inputs.jsonl --tools examples/tools.json --answer tool \
  --old mock:correct --new mock:wrong_tool --draws 10 --batch 1 --out runs/tools
migration-check report --runs runs/tools --out tools.html
```

- `examples/tool_inputs.jsonl` holds clearly fictional support chats (the `examples/` folder is in the repository, not in the installed package). Replace the mock arms with `openai:MODEL`, `openai-compatible:MODEL` or `anthropic:MODEL`.
- Tool names plus `none` replace `--labels`; optional `gold` is a tool name or `none`. Tool mode requires `--batch 1`.
- Definitions accept nested Chat Completions or flat Responses function formats. Names must match `^[A-Za-z0-9_-]{1,64}$`, be unique and not be `none`; `parameters.type` must be `object`.
- Empty replies, unknown tool names, refusals, truncations and filtered replies are failed answers and named hazards. Multiple calls are a hazard; the first call is the answer.
- Argument checks cover required keys, nested basic types, boolean subschemas, tuple items, `patternProperties` and `additionalProperties`, not full JSON Schema. Raw arguments stay only in the local run capture, even with `--show-text`.

### Statistics

- With gold: per-draw accuracy, the accuracy difference D\* (new minus old, in percentage points), primary two-sided min-p slice tests, and stable review candidates (correct in at least 80% of draws on one model and at most 20% on the other).
- Without gold: a total-variation permutation test of both answer distributions per item (10,000 seed-0 samples, inclusive +1 Monte Carlo p, exact enumeration where small enough), then BH across items. CHANGED needs differing modal answers and adjusted p at most 0.05. Mode ties use a deterministic order.
- The smallest achievable per-item p is `2 / choose(2R, R)`: R=2 gives 1/3, R=3 gives 0.1, R=4 gives 0.0286. Below R=4 no item can reach significance; reports show the bound and a recommended R.
- Slice tests assume items are exchangeable; correlated batch failures can break that, so flags need review. NumPy is optional; with it, slice permutations match the reference RNG.

### Mock modes

`mock:` modes are for tests and demos. Text modes: `old` and `new` (first and second label), `flaky`, `snake`, `case`, `extra`, `partial`, `fail`, `retry`, `mismatch`, `self-correct` (batched replies hold a JSON answer, a "Wait, message N is ..." note and a second object correcting the last item: the full object on even draws, only that key on odd draws), and `sample-old` / `sample-new`, the keyword matchers behind `sample --offline` (5% run-to-run noise each; `sample-new` changes 10% of items, one `sample-old` batch comes back upper-cased in the second draw, and the second `sample-new` batch is unparseable in two of every three draws). Tool modes: `correct`, `wrong_tool`, `none`, `bad_args`, `flaky`. Mocks never see gold.

### Tests

```sh
python3 -m unittest discover -s tests -q
```

Tests that run a provider path block network access, and nothing outside the repository is needed. See TESTING.md for what is tested how.
