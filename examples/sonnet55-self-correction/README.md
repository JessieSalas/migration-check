# Reproducer: Claude Sonnet 5.5 answers, writes "Wait", and answers again

How common it is: in a preregistered prevalence study on 1 Nov 2026, Claude Sonnet 5.5 returned two or more JSON
answer objects in 5 of 216 first-attempt calls (2.3%, Wilson 95% interval 1.0% to 5.3%; 3 of 108 batches), against 0
of 216 for Claude Sonnet 4.5. Those calls used migration-check v0.4.1's default prompt at up to 20 messages per call on
public data (Banking77, HWU64 and CLINC150) through the Claude Code CLI at its defaults. That is a rate on public
benchmark data in one configuration, not a production rate.

The six messages here are a searched trigger set, narrowed from a batch that failed repeatedly. The 17 of 20 below is
the rate on that set, chosen because it fails, so use it to reproduce the behaviour, not to estimate how often it
happens.

Six public CLINC150 messages, one batched call, a JSON-only answer. At the Claude Code CLI's defaults (no extended
thinking), Claude Sonnet 5.5 often writes a complete JSON answer, then "Wait", then a second JSON object that revises
one message ("my list of reminders contains what"). A parser that expects one JSON object reads nothing from that reply.
Claude Sonnet 4.5 did not do this on the same inputs.

```bash
migration-check run --inputs examples/sonnet55-self-correction/inputs.jsonl \
  --labels examples/sonnet55-self-correction/labels.txt \
  --old claude-cli:claude-sonnet-4-5-20250929 --new claude-cli:claude-sonnet-5-5 \
  --draws 10 --batch 6 --out runs-sonnet55
migration-check report --runs runs-sonnet55 --out sonnet55.html
```

What we measured on 1 Nov 2026 (preregistered, then recomputed blind on a second machine):

- A separate harness, one call per measurement, fresh randomized calls:
  - Sonnet 5.5 failed to parse in 17 of 20 calls on these 6 messages.
  - Two sets of 6 matched messages from batches that never failed: 0 of 20 each.
  - The same 6 on Sonnet 4.5: 0 of 20. The same messages sent one at a time: 0 of 18.
  - Difference +85 points, 95% interval +58.5 to +94.8.
- With this tool's own prompt (10 draws): Sonnet 5.5 failed 29 of 30 attempts, and merging the JSON objects recovered every answer correctly. Sonnet 4.5 was clean.
- Reading the last complete JSON object gave 118 of 120 correct answers on the harness calls. Raising effort to high only cut failures to 11 of 20.
- In those runs, 5.5 failed only on calls where it used no thinking tokens (28 of 71) and never when it did think (0 of 9). That is observational, not a controlled test.

Scope: this is the tested configuration (Claude Code CLI defaults, which add their own system prompt, no extended
thinking), not every Sonnet 5.5 deployment. API users can likely rule it out with structured outputs; we did not test
that path. The 6 messages are a reproducible trigger set, not representative traffic. Results vary from run to run,
which is the point of running many draws.
