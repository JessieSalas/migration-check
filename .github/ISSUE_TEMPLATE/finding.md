---
name: Finding from a migration-check run
about: Share what migration-check found on your own inputs, without sharing the inputs
labels: finding
---

Please share only the fields below. Never paste `inputs.jsonl`, `calls.jsonl`, `privacy-salt`, raw model replies, a report built with `--show-text`, or `manifest.json` unless your system prompt and labels are fine to publish. Reports and decision records list your item IDs, slice names, gold labels and label list, so remove any of those that identify a customer.

**migration-check version** (`migration-check --version`):

**--old and --new:**

**Served model IDs** ("Served:" on each model card, or `served_models` in `report.analysis.json`):

**Draws and batch size:**

**Number of inputs, and whether they have gold labels:**

**The "Look first" line the command printed:**

**Format-hazard lines (including any Self-correction line) and the accuracy (D\*) line from the top of `report.decision.md`:**

**What you found, in your own words** (for example a format failure, a stable regression, or a hazard that only appears at your batch size):

**Anything that went wrong with the tool itself** (error messages, with any input text removed):
