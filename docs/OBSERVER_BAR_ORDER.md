# Closed-bar logical-time invariant

Follow-up to ec7ce32. This changes no investment rule, threshold, account,
transport allowance, clock bound or EXIT retry policy.

Both ordinary bar ingestion and pending release now use `Ledger.bar_rejection`:
same-open duplicates/conflicts retain their existing outcomes; otherwise a bar
older than the latest accepted bar **for that symbol**, ordered by its original
kline open/close, is rejected as `OUT_OF_ORDER_BAR_TIME`. Receipt time, event E and
release time do not determine the bar's position in market history. Original raw
bytes, receipt and initial interpretation remain in the append-only ledger.

The derived `input_index` cannot regress on replay. The `item_index` also ignores
well-formed old-bar interpretations so that a late UNCERTAIN duplicate cannot
invalidate an already accepted newer bar. This does not suppress a newer uncertain
bar or another symbol. The wildcard index retains its last-accepted-input meaning
across symbols; ordering and investment features are per symbol.

The independent verifier reconstructs bar chronology from original kline fields,
checks selected decision inputs, terminal reasons and exported SQLite projections.
It rejects the old failing ledgers even though their original chains are valid.

```text
python -B -X utf8 scripts/run_sentinel_offline_tests.py --log observer.txt
python -B -X utf8 scripts/run_sentinel_offline_tests.py --pattern "test*.py" --log full.txt
python -B -X utf8 scripts/replay_bar_order.py --out NEW_AFTER_DIRECTORY
python -B -X utf8 scripts/verify_bar_order.py --before BASELINE_REPLAY --after NEW_AFTER_DIRECTORY
python -B -X utf8 scripts/tamper_bar_order.py --after NEW_AFTER_DIRECTORY --out NEW_TAMPER_DIRECTORY
python -B -X utf8 scripts/verify_bar_order_bundle.py EXTRACTED_REVIEW_ZIP
```

The delivered baseline-source contains ec7ce32's exact Observer execution bytes
plus the same synthetic fixture exporter, allowing the failure to be reproduced.
All new data here are offline synthetic fixtures, not market observations. Public
collection, deployment, push/PR/merge, models, notifications, orders and scheduler
changes remain unperformed; a real 5-minute trial needs separate approval.
