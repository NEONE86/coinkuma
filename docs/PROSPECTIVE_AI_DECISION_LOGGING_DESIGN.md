# Prospective AI decision logging — design only

This design is separate from the capacity change and the next collection run.
No model client, inference call, order, scheduler, or deployment is enabled.
The existing `ai_native` decision/policy and PAPER execution paths should be
reused after their timing/provenance interfaces are reviewed; do not duplicate
them with a second trading engine. The Bybit capture and independent source
auditor remain usable without any AI dependency.

## Append-only event contract proposed for a later implementation

Use a separate private decision ledger, never add decisions to old source files.
Every event has schema version, run ID, event ID, UTC receipt time, monotonic
session/time, previous-event hash, canonical payload hash and producer version.
Record model/prompt/policy versions and the approved budget identity at run start.

1. `input_frozen`: frame ID, frame bytes SHA256, raw receipt IDs/hashes,
   raw cutoff and actual availability timestamp. Freeze the entire input bundle,
   economic evidence, missing-data flags and mature-memory IDs before inference.
   Every referenced item must have been received by this cutoff; historical
   publication time alone cannot establish contemporaneous availability.
2. `decision_requested`: idempotent request ID, policy ID, input bundle hash,
   request start, deadline, provider/model parameters and reserved cost. All
   three policies share the same eligible input and evaluation horizon.
3. `decision_received` or `decision_failed`: exact response bytes/hash, actual
   completion time, provider usage and reconciled cost (including failed/retried
   calls). Timeout/late responses remain recorded but are ineligible for action.
   Restart recovery must retrieve a completed response rather than silently
   call again. Unknown provider completion is a distinct unresolved state.
4. `proposal_validated` or `proposal_rejected`: deterministic schema and asset
   checks, input/response identities, policy action, expiry and rejection reason.
   Record facts, inference and assumptions separately. A proposal is not a fill.
5. `paper_execution`: reuse the PAPER broker only on a quote received after
   response completion plus the fixed execution delay. Preserve bid/ask, size,
   fees, slippage/funding assumptions, available capital and no-fill reason.
   No same-input-price fill and no backdated action on restart.
6. `outcome_matured`: fixed horizon/exit rule, later price evidence, net PnL,
   capital/time exposure, model costs and independent evaluator version.
   Pending outcomes and abstentions are reported separately from accuracy.

## Fair comparison and independent verification

Register price-only, economic AI and mature-memory AI policy versions before the
cohort starts. Record CASH/no-action as an explicit decision. Charge each policy
its own calls and execution costs; report gross/net returns and coverage alongside
cash. Mature-memory eligibility uses outcome-observed time, not just event time.
Never edit an active cohort's rule or use this already-observed dataset to claim
prospective AI superiority. Later inference on captured data is retrospective.

A later independent verifier must import neither model client nor decision
producer. It should recompute hashes, input availability, request/response order,
session boundaries, expiry, duplicate requests, usage charges, eligible fills and
maturity from original bytes. Required negative controls: rehashed future input,
backdated response, late response accepted, reused request ID, missing failed-call
cost, same-packet fill, immature memory/outcome, and altered policy version.

## Implementation gate

Before implementing inference: specify exact model/call budget, evidence sources,
three policy definitions and timing schema in a separate reviewed PR. Add offline
normal/adversarial tests first. Obtain explicit execution approval for model calls.
Source consistency and this design do not establish investment profitability.
