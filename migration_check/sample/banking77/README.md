# Bundled live sample: 100 Banking77 tickets

`migration-check sample` (without `--offline`) runs these 100 public customer-support utterances from Banking77,
10 for each of 10 intents, against the full list of 77 Banking77 labels. Licence and attribution: see `NOTICE`
(CC BY 4.0, not MIT).

The 10 intents were chosen from our own one-pair study (Sonnet 4.5 vs Sonnet 5.5 through the Claude CLI, 10 runs
each, exploratory on Banking77): 4 intents that got worse beyond run-to-run noise in that study
(transfer_into_account, verify_my_identity, card_delivery_estimate, card_not_working), 3 that got better
(apple_pay_or_google_pay, card_arrival, supported_cards_and_currencies) and 3 with no flagged change
(lost_or_stolen_card, request_refund, failed_transfer). So this sample is picked to show movement; it says nothing
about how often such changes happen on other data. The sample's prompt and harness are migration-check's own, not
the study's, and 3 draws per model is a demonstration, not an audit, so expect its numbers to differ from the study.
