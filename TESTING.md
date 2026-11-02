# Testing

What is tested, how, and what is not.

## Run the tests

```sh
python3 -m unittest discover -s tests -q
```

Standard library only. Every test that runs a provider path blocks network access (socket connects raise), so nothing reaches a provider; the pure statistics tests touch no I/O. The NumPy reference-RNG comparison is skipped with a reason when NumPy is not installed, and the browser Markdown comparison is skipped when Node is not installed, so the skip count depends on the machine.

At v0.4.1 the suite has 110 tests (109 run and pass on a laptop with Node and without NumPy, 1 skipped), in about 40 to 75 seconds depending on the Python: 43 s on Homebrew Python 3.11.6 and 70 s on Python 3.9.6, the `python3` that ships with macOS, on 1 Nov 2026. (v0.4.0 had 104.)

## What is tested how

| Area                                                                                                                                          | How                                                                                                                                                                                                                                                     | Where                                                                             |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Input, label and gold validation; the batched JSON answer format; parsing                                                                     | unit tests on synthetic inputs                                                                                                                                                                                                                          | `tests/test_migration_check.py`, `tests/test_fixes.py`                            |
| Run capture, retries, early stop, Ctrl-C, torn files, reuse refusal                                                                           | end to end with `mock:` providers                                                                                                                                                                                                                       | `tests/test_migration_check.py`, `tests/test_fixes.py`, `tests/test_v02_fixes.py` |
| Statistics (accuracy difference, min-p slices, total-variation permutation test, BH)                                                          | exact values, calibration across 200 identical-distribution mock audits, and vendored reference numbers                                                                                                                                                 | `tests/test_migration_check.py`, `tests/test_fixes.py`                            |
| Reports and decision records, including privacy (no input text without `--show-text`, pseudonymised free text) and reviewer-edit preservation | end to end with `mock:` providers; the browser Markdown export is compared with the Python one under Node when Node is installed                                                                                                                        | `tests/test_migration_check.py`, `tests/test_fixes.py`, `tests/test_archive.py`   |
| Tool-choice mode                                                                                                                              | end to end with tool mocks and stubbed provider responses                                                                                                                                                                                               | `tests/test_tools.py`, `tests/test_v02_fixes.py`                                  |
| Archive and replay                                                                                                                            | end to end with `mock:` providers, including tampering and forgery cases                                                                                                                                                                                | `tests/test_archive.py`                                                           |
| Self-correction hazard (two or more JSON answer objects in one reply: depth-0 scanning, so arrays, nested or malformed objects and format examples are not counted; merge recovery where a later null never erases an answer; retry outcomes; report note and column; archives keep or recompute the counts, or say not recorded; older captures without the keys; `mock:self-correct`) | unit tests on hand-written replies, and end to end with `mock:` providers and archive replay | `tests/test_self_correction.py` |
| Parse policies (strict parse success for calls and answers, strict accuracy over all answers, accuracy among parsed answers only, strict without retries, last-object recovery, object-merge recovery; each with its denominator, never mixed; recovery never reads a refused or truncated reply; an archive without reply text says not recorded; no recovery rows for batch 1 or tool mode; the table and the rewritten self-correction sentences in all three reports) | offline end to end on hand-written captures, including one shaped like the live 6-message demo, and `mock:` runs with archive replay | `tests/test_parse_policies.py` |
| `sample` subcommand                                                                                                                           | offline end to end (all artefacts, SIMULATED labels, non-trivial report, ticket text and gold on every queued item), missing `claude` CLI (clear message with install pointer, exit 2, no files), a failed start leaves nothing behind and closes the progress line, default model pair, separate default folders for offline and live, bundled data read and validated (the live default is the Banking77 subset: 10 tickets for each of 10 intents, all 77 labels, its NOTICE present), `--draws` override, reruns never overwrite earlier output; that the sample ships inside the installed package is checked by the fresh-clone test | `tests/test_sample.py`                                                            |

### Provider adapters

| Provider             | Status                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `mock:`              | unit-tested                                                                                                                                   |
| `claude-cli:`        | command line, flags, environment and envelope parsing unit-tested with a stubbed subprocess; live-tested on 1 Nov 2026 (see the live run record below) |
| `anthropic:`         | tested against stubbed HTTP responses only; not yet live-tested                                                                               |
| `openai:`            | tested against stubbed HTTP responses only; not yet live-tested                                                                               |
| `openai-compatible:` | tested against stubbed HTTP responses; live-tested against Together AI on 1 Nov 2026 (see v0.4.2 below)                                       |

"Stubbed" means the tests replace the HTTP call with recorded-shape JSON (fixtures in `tests/fixtures/`). They prove request building and response parsing against the documented formats, not that a live endpoint accepts the request today.

## Fresh-clone test

`scripts/fresh-clone-test.sh SOURCE offline|live [local|network]` does what a new user does, in a new temporary directory:

1. Get the code. A git URL (including `file:///path` for a local repository) is cloned with `git clone`, and the clone's `git ls-files` must equal its `PUBLISH-MANIFEST.txt`. A local directory is copied: exactly the files in its `PUBLISH-MANIFEST.txt`, and the script fails if any listed file is missing or if the working tree holds any other publishable file.
2. Install into a new venv. `local` (the default) installs with no network (`--no-index --no-deps --no-build-isolation`, reusing the system setuptools through `--system-site-packages`). `network` installs with pip's normal build isolation, as pipx and pip do for a stranger, so it fetches the current setuptools from PyPI.
3. Check the install: the distribution is named `migration-check` with the package's version, the package is imported from the install rather than the checkout, and the bundled sample and `report.html` are inside it.
4. Run `migration-check sample` (with `--offline` in offline mode) from outside the source tree, and check that `report.html`, `report.decision.json`, `report.decision.md` and `report.analysis.json` exist and are not empty, and are labelled simulated exactly when they should be.
5. In live mode, also fail if either model has no served model ID, if the report has an adapter warning (provider HTTP or unreachable errors, `claude-cli error` or invalid envelope, every call failed, attempts without a served ID), or if a new `~/.claude/projects/*migration-check-*` transcript directory appeared during the run.
6. Run the unit tests in the clone.

It ends with:

```
FRESH-CLONE OK mode=MODE install=INSTALL clone_to_report=Ns sample_to_report=Ns tests=N
```

## Publishing

1. `scripts/export-public.sh DEST` copies exactly the files in `PUBLISH-MANIFEST.txt` into a new directory with fresh git history (it stages them; add `--commit`, or run the printed commit command, to make the single first commit). Never push this repository's own history.
2. `scripts/fresh-clone-test.sh file://DEST offline network` and `scripts/fresh-clone-test.sh file://DEST live` test the export through a real clone.
3. Fill in the live run record below and the claude-cli "Tested" cells in README.md and the table above. The README placeholders were filled in at first publication (1 Nov 2026).
4. `scripts/prepublish-check.sh DEST` must print `PREPUBLISH OK` before the repository is pushed.

## Live run record

### v0.4.1 (current)

- Date: Sun 1 Nov 2026, 10:31 to 10:33 PST.
- migration-check version and commit: 0.4.1, source commit 065ae34 exported with `scripts/export-public.sh` (a fresh-history export, so its own commit hash does not survive a re-export). Only README.md and TESTING.md changed afterwards, to record this run.
- Machine and OS: Apple M2 MacBook Air, macOS 27.2 (26B5086k), Homebrew Python 3.11.6 for the venv (created with `--system-site-packages`, as the script's local mode does).
- Claude Code CLI version (`claude --version`): 2.1.310.
- Command: `scripts/fresh-clone-test.sh file://<export> live` (real `git clone` of the export, fresh venv, local install, unit tests, then `migration-check sample`).
- FRESH-CLONE line: `FRESH-CLONE OK mode=live install=local clone_to_report=56.9s sample_to_report=52.2s tests=110`.
- Served model IDs: old `claude-sonnet-4-5-20250929`, new `claude-sonnet-5-5`, exactly as requested, on all 30 attempts (30 calls, no retries).
- Adapter errors or warnings in the report header: none. No format hazards; per-draw accuracy 85, 88 and 88% (old) and 83, 83 and 82% (new), overall 87.0% and 82.7%; 7 items in the review queue.
- Transcript directories under `~/.claude/projects/*migration-check-*` before and after: 120 and 120 (no new transcripts).

### v0.4.0 (earlier, kept for the record)

- Date: Sun 1 Nov 2026, 09:52 to 09:54 PST.
- migration-check version and commit: 0.4.0, source commit 1b63cca exported with `scripts/export-public.sh` (a fresh-history export, so its own commit hash does not survive a re-export). README.md and TESTING.md were edited afterwards to record this run. v0.4.1 changed only the self-correction scanner, its counts in analysis, reports and archives, and the sample's printed notes; the claude-cli adapter is unchanged, and v0.4.1 itself has not had a live run.
- Machine and OS: Apple M2 MacBook Air, macOS 27.2 (26B5086k), Homebrew Python 3.11.6 for the venv (created with `--system-site-packages`, as the script's local mode does).
- Claude Code CLI version (`claude --version`): 2.1.310.
- Command: `scripts/fresh-clone-test.sh file://<export> live` (real `git clone` of the export, fresh venv, local install, then `migration-check sample`).
- FRESH-CLONE line: `FRESH-CLONE OK mode=live install=local clone_to_report=116.3s sample_to_report=111.9s tests=104`.
- Served model IDs: old `claude-sonnet-4-5-20250929`, new `claude-sonnet-5-5`, exactly as requested, on every attempt.
- Adapter errors or warnings in the report header: none. No format hazards in this run; 7 items in the review queue.
- Transcript directories under `~/.claude/projects/*migration-check-*` before and after: 120 and 120 (no new transcripts).
- Notes: an earlier live run of the same sample from a working-tree copy made before the self-correction hazard landed (09:45 PST; it could not detect that hazard, though its report says 0.4.0) took 96.2 s, with all 30 calls answered on the first attempt, accuracy 86.3% (old) and 84.3% (new), and 4 queued items. The queue size differs between the two runs (4 and 7) because 3 draws per model is a demonstration, not an audit. The worked example in README.md ("What it tells you") is a separate live `migration-check run` on 6 CLINC150 messages, 20 calls (40 attempts with retries), 09:55 PST, captured and reported at 0.4.0; re-analysing its capture with v0.4.1 gives the same counts (new arm: 10 self-correction calls, 9 recovered for 54 of 54 answers, 1 parsed on retry). The installed-from-network path (`pip`/`pipx` from a public URL) has not been run, because the public repository does not exist yet.

## v0.4.2 (1 Nov 2026)

- First live run of the `openai-compatible` adapter, against Together AI (`https://api.together.xyz/v1`): DeepSeek V4.1 Flash vs DeepSeek V4.1 Flash 1017 on 6 CLINC150 messages, 1 draw. Served model IDs were exact and every answer parsed.
- Bug found by that run and fixed: Together answers HTTP 403 to urllib's default `Python-urllib/x.y` User-Agent (checked with curl: 403 for that agent, 200 for a named one). Every request now sends `User-Agent: migration-check/<version>`. Test: `tests/test_user_agent.py`.
- Flaky test fixed: `test_incomplete_call_counts_and_duplicate_rejection` dropped the last line of `calls.jsonl`, whose arm depends on parallel worker timing. It failed 2 of 3 runs on v0.4.1 and passes 10 of 10 after dropping the last `new` call explicitly.

## v0.4.3 (1 Nov 2026)

- Why: two independent reviews of the 6-message Sonnet 5.5 demo read "accuracy old 1.000, new 0.417" as a model regression. It is an output-contract failure whose answers are in the replies, and the old self-correction sentence ("in the 1 call whose last attempt was one, merging the objects, ...") did not say so in readable English.
- Reports now carry `parse_policies` (in `report.analysis.json` and `report.decision.json`) and a parse-policy table (in `report.decision.md` and the HTML Format hazards section): per model, strict parse success for calls and answers, strict accuracy over all answers, accuracy among parsed answers only, and accuracy under strict without retries, last-object recovery and object-merge recovery. Each cell states its denominator. Recovery policies re-read stored replies in order and stop at the first that yields an answer, as the run's retries did; they never make a call, and the run's own answers stay strict. The self-correction note is now short sentences that name each policy, and each self-correcting arm also gets `self_correction_outcomes` in its hazard summary. The older `self_correction_recoverable_items` field is unchanged.
- Labels: the headline accuracy is now "Strict accuracy" in all three reports. No count changed.
- Demo re-report, offline from the stored live capture (2 draws, batch 6, 2 retries; `migration-check report` at 0.4.3): new strict parse 1 of 2 calls and 6 of 12 answers; strict accuracy 5 of 12 (41.7%); accuracy among parsed answers 5 of 6 (83.3%); last-object and object-merge recovery 12 of 12 (100.0%) each; old 12 of 12 under every policy.
- Tests: `tests/test_parse_policies.py` adds 6. Four existing tests changed with the wording: three in `tests/test_self_correction.py` (the self-correction sentence; two of them now also set the recorded answers that their synthetic retries claim to parse) and the sample's accuracy line in `tests/test_sample.py`. The suite has 117 tests: 116 pass and 1 is skipped (no NumPy), in 20 s on Python 3.15 and 32 s on Python 3.9.6, on 1 Nov 2026.

## v0.4.4 (1 Nov 2026)

- Why: two independent reviewers of a real run (GLM-5.3 against GLM-5.3-Flash on HWU64, 620 inputs, 5 draws, batch 20, through Together AI) found that the parse-policy row "Accuracy among strictly parsed answers only" equalled strict accuracy (2517 of 3100 for new under both), because off-list answers parse. That row proves nothing when the hazard is a spelling change.
- New table row "Accuracy among on-list answers only (parsed and spelled as an allowed label)": correct over answers that parsed and are on the label list, per model, with denominators. Shown when the run has a label list. `parse_policies` counts gain `on_list`, `on_list_gold` and `on_list_accuracy` for every policy, and `gold.on_list` carries the strict counts with an item-clustered, paired 95% interval for the difference. `gold.Dstar_ci95` is the same kind of interval for strict accuracy.
- New "Where the hazards are" line: per model, how many of the scheduled calls hold the off-list and the missing answers, with the off-list pattern counts and whether each affected call was entirely off-list. `hazard_concentration` in the analysis JSON holds the counts.
- New decision summary: one paragraph generated from the numbers, at the top of `report.decision.md` (after any SIMULATED or ARCHIVED provenance lines), in `report.decision.json` as `decision_summary`, and at the top of the HTML page. It says "an output-format change, not a loss of task accuracy on the answers that follow the format" only when new's on-list accuracy is at or above old's, or the 95% interval of the difference includes zero (it then quotes the interval); otherwise it says both the format and task accuracy changed. When answers fall off the list it states that strict accuracy stays authoritative while the output contract is unchanged. It says "no regression detected across the tested inputs and draws" only when strict accuracy does not drop beyond its interval, the new model has no missing or off-list answers, no input without gold changed, and the review queue is empty. The tool emitted no "safe" wording before this release; README now says what it says instead.
- Off-list examples: up to 3 per model, most frequent first, each beside the closest allowed label (case, underscores and hyphens ignored, then `difflib` similarity), only in reports built with `--show-text`. Decision: off-list answers are model output, which can contain input text, so they follow the existing default for non-label answers (hidden, pseudonymised where shown). Without `--show-text` the report says how to see them. An archived old arm has no examples, because its answers are stored pseudonymised.
- Intervals: normal approximation, ratio estimates linearised per input, so an input's repeated draws form one cluster and its old and new answers stay paired. Stated in the report's limitations. On-list accuracy is descriptive, since a model's off-list inputs leave its denominator.
- Real-run re-report, offline from the stored capture (`migration-check report` at 0.4.4, no provider call): strict accuracy old 2680 of 3100 (86.5%), new 2517 of 3100 (81.2%), D* -5.3 points (95% interval -7.1 to -3.4); among on-list answers old 2680 of 3100 (86.5%), new 2517 of 2880 (87.4%), difference +0.9 points (95% interval -0.6 to +2.5); new has 220 off-list answers (all snake_case), all in 11 of 155 calls, every answer in each of those calls; old has none. With `--show-text`, the top examples are `datetime_query`, `social_query` and `general_joke`, each beside its spaced label.
- Tests: `tests/test_decision_summary.py` adds 9 (the on-list row and its absence without labels, the concentration line for whole-call, partial and missing cases, the summary's format, both-changed, no-regression, missing-answer and no-gold branches, the `--show-text` gate on examples, and the interval's clustering). One existing test changed: the exact strict-count dictionary in `tests/test_parse_policies.py` gains the three on-list keys. The suite has 126 tests: 125 pass and 1 is skipped (no NumPy), in 20 s on Python 3.15, on 1 Nov 2026.

## v0.4.5 (1 Nov 2026)

- Why: a live rerun of `examples/sonnet55-self-correction` from a fresh clone (3 draws, Claude Code CLI, 13 s) gave new 5 correct of 6 on-list answers against old 18 of 18, a difference of -16.7 points with a 95% interval of -49.3 to +16.0. The v0.4.4 summary read that as "not a loss of task accuracy", a firmer claim than an interval that wide supports.
- Change: the summary keeps "an output-format change, not a loss of task accuracy on the answers that follow the format" only when new's on-list accuracy is at or above old's. When it is lower and the interval includes zero, it now says "the drop is mostly an output-format change; no loss of task accuracy on the answers that follow the format is detected, but a loss of up to N points is not ruled out", with N the interval's lower end. The both-changed branch is unchanged.
- Tests: `tests/test_decision_summary.py` adds 1 (the at-or-above branch) and updates the two cases with a lower on-list accuracy to the new wording.

## v0.4.6 (1 Nov 2026)

- Why: VP review of a real run (GLM-5.3 against GLM-5.3-Flash on HWU64, re-reported offline from the stored capture). Even when new's on-list accuracy is at or above old's, "not a loss of task accuracy" over-reads a selected subset: inputs a model answers off-list leave its denominator. The review also asked the summary to lead with the calls to fix, which the hazard line already counted.
- Change: the at-or-above branch now reads "The drop is mostly an output-format change: among answers that follow the format, no loss of task accuracy is detected (difference D points, 95% interval L to H points)", as its own sentence after the on-list accuracy numbers. The v0.4.5 lower-and-wide branch and the both-changed branch are unchanged. The summary no longer emits "not a loss of task accuracy" in any branch.
- New sentence: when the new model's off-list answers sit in at most half of its calls, the summary adds "Fix first: the N calls that hold the off-list answers." right after the hazard sentence. Missing answers alone do not trigger it.
- Real-run re-report (`migration-check report` at 0.4.6, no provider call): "... All answers parse; 220 of 3100 use labels outside the allowed set, in 11 of 155 calls. Fix first: the 11 calls that hold the off-list answers. Among on-list answers, accuracy is 87.4% (old 86.5%). The drop is mostly an output-format change: among answers that follow the format, no loss of task accuracy is detected (difference +0.9 points, 95% interval -0.6 to +2.5 points). ..."
- Tests: `tests/test_decision_summary.py` adds 1 (no "Fix first" when off-list answers are in every call, or when only answers are missing) and updates the `FORMAT` constant and the two summaries that now carry "Fix first". The suite has 128 tests: 127 pass and 1 is skipped (no NumPy), in 21 s on Python 3.15, on 1 Nov 2026.
