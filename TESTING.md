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
| `sample` subcommand                                                                                                                           | offline end to end (all artefacts, SIMULATED labels, non-trivial report, ticket text and gold on every queued item), missing `claude` CLI (clear message with install pointer, exit 2, no files), a failed start leaves nothing behind and closes the progress line, default model pair, separate default folders for offline and live, bundled data read and validated (the live default is the Banking77 subset: 10 tickets for each of 10 intents, all 77 labels, its NOTICE present), `--draws` override, reruns never overwrite earlier output; that the sample ships inside the installed package is checked by the fresh-clone test | `tests/test_sample.py`                                                            |

### Provider adapters

| Provider             | Status                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `mock:`              | unit-tested                                                                                                                                   |
| `claude-cli:`        | command line, flags, environment and envelope parsing unit-tested with a stubbed subprocess; live-tested on 1 Nov 2026 (see the live run record below) |
| `anthropic:`         | tested against stubbed HTTP responses only; not yet live-tested                                                                               |
| `openai:`            | tested against stubbed HTTP responses only; not yet live-tested                                                                               |
| `openai-compatible:` | tested against stubbed HTTP responses only; not yet live-tested                                                                               |

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
