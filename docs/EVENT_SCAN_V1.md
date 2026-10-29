# Event scan and existing-position maintenance

This change is based on PR13 commit 6bf11fe26815b4f2dae98008650a9eb679a76a9d.
It preserves the sealed Alpha modules, allocations, original 13-clock results,
source archives, failures and shared authority budgets.

Existing paper positions use durable operational intents and HISTORICAL/research
resource accounting. They do not call begin_forward or consume significance
allocations. The controller's due_label guards remain unchanged. Only a task
bound to stored filled positions may service those positions during a research
halt, subject to the original available resource caps. A crash without a receipt
charges the full 240-second reservation. Overruns remain unreconciled and halted.
Canonical receipt results exclude storage metadata. Maintenance does not set the
research cadence. Alpha maturity and position servicing still run first.

EVENT_SCAN_V1 is a new sampling specification on previously used source receipts.
It examines all eligible completed hourly contexts in chronological order. Each
policy sees the same clocks and has one independent fixed-notional position:
10,000 USDT capital, 12.5% exposure, two-hour entry delay, 24-hour holding period,
10 bps fee + 5 bps slippage + 1 bp half-spread per side, spot funding zero.
An accepted signal blocks another entry until exit, including entry delay.
The absorption and cross-asset delay predicates and tie-breaks are unchanged.
Shared 26-hour signal-to-exit clusters across both mechanisms and common raw
lineage are recorded; neither signals nor clusters prove statistical independence.
Received-at timestamps are distinct from historical signal/entry/exit clocks.
Hourly open prices are reconstructions, not executable quotes. Missing coverage
is explicit; the future price value cannot affect the historical feature.

AI is NOT_RUN/null under the existing child model_calls=0 cap. Deterministic
successors distinguish no events, negative net, positive exploration, missing
data, execution failure and expiry. Diagnostic actions consume the same queue
and resource controller, but are not investment trials. A positive historical
result only freezes a forward candidate; registration and significance spending
for that future cohort require a separate reviewed protocol. Missing-data
follow-ups list the precise deficiency; no unspecified paid repair is executed.
There is no unconditional mechanism ring for event results. Completed diagnostics
wait for new evidence/review instead of recursively creating empty work.

Operational queue revisions preserve old specs and record a replacement with the
same raw inputs, economic rule, due/expiry clocks and limits. Only the executor
version changes. Original queued records are not deleted or silently rebound.

Reproduce, using the existing runtime (never create a replacement authority):

```text
python -B -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests
python one.py --help
python one.py status --workspace workspace_public
python -m one_trading.economic_batches --runtime RUNTIME revise-implementation --revision-reason "PR13 review: maintenance and event scan"
python -m one_trading.economic_batches --runtime RUNTIME register-event-scan
python -m one_trading.economic_batches --runtime RUNTIME run-queue --request-id pr13-event-scan-20261029 --max-items 2
python -m one_trading.economic_batches --runtime RUNTIME run-queue --request-id pr13-event-scan-20261029 --max-items 2
```

The last command must return CACHED. The existing Windows wrapper runs
alpha-service, growth-service, recover, run-queue in that order. The review ZIP
contains exact-byte source versions, old and new exports, raw/model artifacts,
receipt/status coverage, synthetic recovery logs and independent stdlib scripts:

```text
python latest/verify_research_transitions.py latest
python first-batch/verify_economic_batch_bundle.py first-batch
```

No live orders, transfers, new paid services, deployment or PR merges are added.
Synthetic tests demonstrate software behavior, not investment advantage.
