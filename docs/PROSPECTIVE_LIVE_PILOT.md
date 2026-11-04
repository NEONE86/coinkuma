# Finite Bybit / decision / PAPER integration

This follow-up stacks on PR #22 without merging or changing any prior PR.
Default execution uses `FREE_PROBE_NOT_AI`: external model calls and model
spend are zero. Deterministic probe entry/exit is connectivity evidence only.
The AI-named comparison sleeve is explicitly a stand-in in that mode.

## Reuse and boundaries

PRs #10–#17 are ancestors of the input branch: their research, maturity,
append-only source and cost mechanisms remain unchanged. No sealed cohort,
old forecast or scheduler is reopened. PR #18 supplies `Experiment`, `Protocol`,
`CashPlanner`, `MomentumPlanner` and independent PAPER account audit.
PRs #19–#20 supply the unchanged isolated Bybit spot collector and source audit.
PR #22 supplies input/response chronology, budgets, original response bytes and
independent source binding. Its original replay now also propagates expiry.
PR #21 was inspected: it uses linear perpetual tape, 3x leverage and different
execution assumptions. It is a sibling branch, not an ancestor. Mixing that
leveraged engine into this unlevered spot cohort would change the experiment;
its implementation and evidence remain separate.

## Execution

Run from an exact **clean committed checkout**. New private parent directory must
exist; runtime must be outside Git. On Windows use a user-only inherited ACL.

```powershell
python -B -X utf8 -m one_trading.ai_native.prospective_live `
  --output-dir C:/private/new-run-id --source-commit EXACT_COMMIT `
  --duration-seconds 90 --max-bytes 10000000 --max-calls 4 `
  --decision-interval-seconds 15 --cost-limit-usdt 0 --enable-public-network
python -B -X utf8 -m one_trading.ai_native.live_audit --run-dir C:/private/new-run-id
```

`max-calls` bounds local probe responses in free mode, not actual model calls.
It bounds external attempts only with an explicitly authorized model approval.
No existing runtime is resumed or overwritten. An uncertain model response
stops new requests; it remains unresolved, never becomes a free failed call.
Source receipt collection continues in its own finite child process while a
model waits. No automatic collector restart, scheduler or deployment is added.

Each eligible epoch has one common frozen source frame for all three sleeves.
Between epochs, quotes still settle pending orders and mature labels; no arm
gets extra decision opportunities. Real model latency delays only that arm's
eligibility. Input context, original response, request/receipt/validation UTC
and elapsed time are preserved. A later quote must satisfy the minimum delay,
freshness, visible depth, spread, portfolio risk and proposal expiry checks.
AI BUY/SELL/HOLD controls entry, partial/full exit or stop-loss decisions;
there is no fabricated fill or automatic forced close at the pilot end.
Remaining exposure is marked at bid and reported separately from realized PnL.
No execution advantage is claimed from L1 modeled fills.

Model charges debit PAPER cash before later fills and position sizing. The
charge becomes available only at the recorded response/validation time; quotes
received while inference was still pending cannot see that future cost. An
unsettled end-of-run charge is explicit and prevents a complete live audit.
Non-epoch frames are `decision_skipped`, not model failures.
The
independent verifier recalculates context from cash/positions/history, checks
the exact response plan and timing against PAPER decisions, recomputes controls,
fees/slippage/capital, and reconciles provider usage at pinned approved rates.
Usage-based API expense is **not a provider invoice**. Unknown completion is
INCOMPLETE, not profitable zero-cost execution. Historical replay diagnostics
in the original PR22 bridge remain explicitly post-hoc and separate.

## Actual-model approval gate (currently unapproved)

Current user authorization: **0 external calls / 0 USDT**, no inherited budget.
After the zero-cost test passes, propose a new finite pilot with an exact commit,
run ID, provider/model, rate source, call cap, per-call reservation, total budget,
USD/USDT convention and timeout. The JSON approval must explicitly authorize
calls and match the executable run. An API key is read from the environment;
never copy it into evidence, logs or Git. No key provisioning occurs here.

Transport reference: [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
The adapter sends only frozen context, requests schema-constrained responses,
disables tools and storage, and does not retry. Refusals/invalid/late answers
cannot generate orders. Model quality, provider access and actual latency need
a separately approved live call; mock transport tests do not establish them.

### Proposed next trial — NOT APPROVED

- Provider: OpenAI Responses API; model snapshot `gpt-5.4-mini-2026-03-17`.
- Maximum 10 calls, 0.50 USDT total, 0.05 USDT reserved per request; no retries.
- Duration 600 seconds, 100 MB public source cap, common decision cadence 30 s.
- Output cap 1,024 tokens, no tools. USD=USDT=1 is an explicit accounting
  assumption, not an exchange-rate measurement or an invoice claim.
- Rates per million tokens: input USD 0.75, cached input 0.075, output 4.50;
  [official model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini),
  checked 2026-11-04. Recheck before any separately approved execution.
- Illustrative 2,000 input + 500 output tokens: USD 0.00375/call, USD 0.0375
  for 10 calls. These are estimates, not measured bills. Actual usage controls
  the recorded expense; the reservation gate can stop earlier.
- Stop new model attempts on exhausted call/cost budget, unknown provider
  completion, input older than 5 seconds, source termination, changed source,
  invalid/expired approval or cost overrun. Late/invalid answers do not fill.
- Approval must bind exact final commit, new run ID, expiry, duration, source
  cap, cadence, model, rates and budget. Existing authorization is still zero.
  No approval JSON with `model_calls_authorized=true` is supplied by this PR.

## Verification / evidence handoff

```text
python -m unittest discover -s tests -v
python -B -X utf8 scripts/run_observer_audit.py --out /private/new-audit
python -B -X utf8 -m one_trading.ai_native.live_audit --run-dir /private/run
```

Preserve config, source manifest and exact source archive, original source files,
decision JSONL, model inputs/provider receipts, incremental PAPER journal and
final PAPER result. Hash files and ZIP; verify PRIVATE repository visibility
before upload, then download and compare the ZIP hash. Public PRs carry identity,
hashes and status only. A checksum proves consistency, not independently
witnessed wall-clock authenticity. Price/economic superiority remains untested
until sufficient separately registered prospective outcomes mature.
