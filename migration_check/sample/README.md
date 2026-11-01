# Bundled sample

This directory holds two samples: the synthetic set described below, used by `sample --offline`, and `banking77/`, 100 public Banking77 tickets used by the live `sample` (CC BY 4.0, not MIT; see `banking77/NOTICE` and `banking77/README.md`).

`tickets.jsonl` holds 100 short customer-support messages for a fictional online bank, each with a gold intent label. `labels.txt` lists the 10 labels, one per line. The synthetic sample exists so you can see the full report and decision record before you run the tool on your own inputs.

## Provenance

Every message is synthetic: it was written for this repository with the help of an AI model and a short assembly script, not drawn from any dataset or real conversation. No third-party data, no real customer text, no real people, companies, banks, phone numbers, emails, card numbers or account numbers appear in it. Amounts, dates and references are made up. Because the sample is original to this repository, it is covered by the repository MIT licence.

## Format

One JSON object per line:

```
{"id": "sample-001", "input": "<message text>", "gold": "<label>", "slice": "<label>"}
```

`slice` equals the gold intent, so the report can break results down per intent. Gold and slice are never sent to a model. From each row only `input` is sent, together with the label list and fixed instructions.

## Labels

10 intents, 10 messages each, in shuffled order (ids sample-001 to sample-100):

- card_lost_or_stolen
- card_not_arrived
- transfer_not_received
- refund_request
- fee_dispute
- change_address
- account_locked
- cancel_subscription
- exchange_rate_question
- close_account

Messages range from 4 to 44 words and from terse to rambling, with some typos, lowercase and politeness, as real support traffic has.

## Borderline items

16 items sit near an intent boundary, so two models can plausibly disagree on them. Each has a defensible gold label, but a different answer on these is a judgement call, not necessarily an error.

| id         | gold                   | competing label        |
| ---------- | ---------------------- | ---------------------- |
| sample-002 | exchange_rate_question | fee_dispute            |
| sample-031 | transfer_not_received  | refund_request         |
| sample-038 | cancel_subscription    | close_account          |
| sample-039 | card_lost_or_stolen    | card_not_arrived       |
| sample-047 | exchange_rate_question | fee_dispute            |
| sample-052 | refund_request         | transfer_not_received  |
| sample-054 | refund_request         | fee_dispute            |
| sample-062 | card_not_arrived       | card_lost_or_stolen    |
| sample-063 | fee_dispute            | exchange_rate_question |
| sample-067 | refund_request         | transfer_not_received  |
| sample-070 | close_account          | cancel_subscription    |
| sample-076 | fee_dispute            | cancel_subscription    |
| sample-081 | account_locked         | close_account          |
| sample-083 | close_account          | account_locked         |
| sample-088 | card_not_arrived       | change_address         |
| sample-098 | card_not_arrived       | card_lost_or_stolen    |

The other 84 items have one clear intent.
