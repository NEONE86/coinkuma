# PR12 follow-up: bounded research transitions

This adapter preserves the sealed Alpha source, protocol, ledgers and shared
authority. It adds owner-scoped task recovery and critical maturity/position
observation before optional sidecar research. An overrun remains CLAIMED with its
original reservation and engine halt. No refund, implicit settlement or new
significance allocation occurs. Existing-case maintenance can reserve remaining
time despite a research halt; it still consumes the same experiment/engine caps.

Model requests persist the exact input, cutoff, schema, prompt and model identity
before invocation. Recovery validates completed artifacts and never repeats an
uncertain invocation. New GROWTH decisions have a frozen ten-minute expiry and
cause-state hash. An expired LONG becomes EXPIRED without a new quote or fill.
Old decisions/results remain intact; a legacy request without a complete contract
is quarantined. A later judgment requires a new batch/decision ID.

The existing scheduler now runs `alpha-service`, `growth-service`, `recover`, then
`run-queue`. It reuses the initialized Alpha runtime through an operational adapter;
it does not initialize another cohort. All later research reads typed immutable
HypothesisSpecs in the existing queue. The three legacy FAMILIES remain only for
first-batch recovery. Model output is never interpreted as executable code.

Example commands from this checkout, with the existing private runtime path:

```text
python -B -m one_trading.economic_batches --runtime RUNTIME --revision-reason "PR12 reviewed operational fixes" revise-implementation
python -B -m one_trading.economic_batches --runtime RUNTIME alpha-service
python -B -m one_trading.economic_batches --runtime RUNTIME --request-id pr12-review-20261029 --max-items 2 run-queue
python -B -m one_trading.economic_batches --runtime RUNTIME run-queue
python -B -m one_trading.economic_batches --runtime RUNTIME accounts
python -m unittest discover -s tests -v
python scripts/verify_economic_batch_bundle.py BUNDLE/first-batch
python scripts/verify_research_transitions.py BUNDLE/latest
```

The explicitly requested follow-up has a durable request ID and the same 3-trial,
360-active-second execution ceilings, 240-second trial cap and shared authority.
It spends zero extra model calls/cash. Automatic requests retain the 24-hour
interval and 14-day window, measured from the latest request. Repeating the same
request ID returns a cache result, never a new experiment. A source mismatch
blocks execution until an explicit operational revision; frozen specs require
their original executor. No existing historical outcome is reinterpreted.

The first child tests aggressive spot selling with elevated volume but small
price impact. Its successor tests delayed transmission from BTC to ETH/SOL.
Both use previously received hourly archives and are historical exploration,
with a two-hour reconstruction delay and 24-hour return. Assumed trading costs
are 10 bps fee + 5 bps slippage + 1 bp half-spread per side. Price-only, economic
rule and cash are compared at fixed notional. AI_UNAVAILABLE is explicitly NOT_RUN;
the parent's CASH answer is not reused as a child's AI prediction. Re-observation
retains archive-overlap and idea lineage, and never counts as independent alpha.

Policy accounting deducts all decision costs, including CASH/WAIT/EXPIRED, once
per model call per counterfactual policy. Provider billing remains unknown; 0.10
USDT per invocation is only sensitivity analysis. Alternative-policy capitals and
shared-call costs must not be summed as an actual account/provider bill. Open
positions do not receive a completed policy-performance rank.

Review exports contain separate first-batch and latest ledgers, original raw
responses/model input/answers, exact operational source bytes, preservation
hashes, explicit personal-path redactions and offline independent recomputation.
The first-batch verifier has deliberately fixed snapshot expectations; the typed
verifier accepts subsequent batches and validates lineage, features, actions,
quantity, fees, results and controller accounting. Hashes establish local
consistency, not external timestamp notarization or investment profitability.
