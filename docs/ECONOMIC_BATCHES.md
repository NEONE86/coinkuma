# Bounded economic batches beside the sealed Alpha cohort

Run from this checkout with the existing authorized model executable on PATH:

```sh
python -B -m one_trading.economic_batches --runtime /private/batch --alpha-runtime /private/alpha init
python -B -m one_trading.economic_batches --runtime /private/batch run
python -B -m one_trading.economic_batches --runtime /private/batch recover
```

The original Alpha runtime, protocol, source bytes, authority ID, capital and
global alpha registry remain authoritative. Match the original checkout's byte
line endings before initializing: a CRLF conversion is a source identity change.
Never disable that check. Use a separate private batch directory. No collected
data, model transcripts or personal filesystem paths belong in Git.

The adapter reuses Store, public HTTP/chain capture, model invocation, paper fill,
and the ResearchLoop task queue/budget controller. Each daily batch has three
distinct causes: received forced inventory flow, financing crowding, and stablecoin
supply/Aave credit demand. It allows at most three trials, two model calls, 90 HTTP
attempts, 240 reserved seconds per trial and 360 active seconds per batch. Existing
zero new cash budgets and 14-day collection window apply. Model cost of 0.10 USDT
per call is an explicit sensitivity assumption, not a measured invoice or bound.
Controller HISTORICAL registration here accounts research resources; it allocates
no new significance claim, including for current GROWTH paper decisions.

Forced-flow reuse has zero new independent units. Historical funding exploration
uses 13 disjoint 24-hour outcomes, spaced 48 hours apart, with a two-hour entry
delay. The price arm buys the weakest negative prior-day return; the economic arm
also requires negative last-three funding sum. One retrospective model selects a
fixed price/funding/cash rule using only the earliest context; later contexts and
outcome prices never enter its prompt. Subsequent daily runs collect new panels
without tuning or rerunning the same backtest. Spot holders receive no perp
funding. Historical half-spread and slippage are assumptions, hourly opens are
not executable quotes, and the model may know subsequent history. Neither these
outcomes nor repeated cached runs qualify as forward AI evidence.

GROWTH records official contract addresses and two finalized Ethereum block
states, canonical rechecks, contract code, decimals and total supply. Joint USDC
supply and Aave variable debt stock growth is a narrow proxy: accrued interest,
net new borrowing, program attribution and AAVE holder capture remain distinct.
HTTP/RPC failures yield UNKNOWN, never false or invented values. Current economic
and price inputs feed native price, AI, delayed AI and cash policies. Two genuinely
post-registration nonoverlapping daily intervals are required for the delayed
economic gate. Actual AI readiness plus two seconds precedes a fresh executable
quote. Existing depth/lot/fee/slippage fill checks apply; at most one open position
per policy prevents daily overlapping exposure. A 30-day or observed 2% adverse
exit is polled, not guaranteed at the threshold. Positions remain unscored until
an actual exit. Unknown economics can legitimately lead to no trade in every arm.

Durable model intent prevents uncertain charged calls being retried; input and
result hashes support caching. Interruption without an execution receipt consumes
the full reserved time conservatively. Suspended adapter tasks cannot be claimed
by the old worker. Incomplete batches resume before new ones; missing inputs and
errors are prioritized in the next queue. The Windows wrapper runs adapter claim
recovery, the unchanged Alpha runner, then this adapter. Existing open Alpha risk
or imminent maturity takes priority. Install it only as the existing paper
research task's action, retaining trigger, identity, privileges and limits.
Initialization alone does not install or promise a scheduler.

Operational fixes can append a source-only revision after all current batches
finish; parameters, original registration time, results and budgets are retained:

```sh
python -B -m one_trading.economic_batches --runtime /private/batch --revision-reason "Describe operational fix" revise-implementation
```

Do not use this command to retune a criterion or re-evaluate old results. Those
changes require a newly registered experiment/version. The design diagnostic
computes constant-positive-return feasibility against the unchanged e-process
threshold; it is not statistical power, investment performance or permission to
lower a prior threshold. Review exports must include raw public response bytes,
source/receive times, immutable ledger records, independent arithmetic and an
explicit redaction map for personal paths. No trading API or deployment is added.
