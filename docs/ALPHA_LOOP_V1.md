# Alpha Loop: first native-time forward experiment

This additive module starts from PR #10, without changing its sealed code,
scheduled runtime, old forecasts or rejected historical strategies. It imports
the supplied Alpha Loop reference control/evidence/VOI code, with relative imports,
Windows constructor-error connection cleanup and an explicit native-time contract.
The original attachment and unmodified reproduction logs remain in the private
review bundle. This is public-data paper research. No real order adapter exists.

## Candidate selection

CF_RESIDUAL_FORCED_FLOW is selected: public spot bars/books plus a public venue
liquidation stream can be captured now, with a six-hour endpoint. Bybit identifies
the liquidated position side, executed base quantity and bankruptcy price. That
price is never a spot fill. See the [primary API specification](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation).
The hypothesized spillover into Binance spot is an assumption, not an observed
causal effect. An initial bounded Binance stream probe without messages is not
proof of zero liquidations.

GROWTH_DEMAND_SUPPLY_CHANGE remains a separate cheap-falsification task. Circle's
published matched-mint incentive can be captured, but attributable mint/borrowing
amounts, aligned coverage and AAVE holder-demand conversion are missing. Economic
and investment windows are much longer. The worker preserves the publisher bytes
and reports DATA_BLOCKED; it does not promote this narrative into a growth trade.

## Frozen contract

`one_trading/alpha_loop/protocol.py` records every numeric research assumption.
Six strategy parameters are explicitly listed, not grid searched: six-hour common
horizon, 168 completed hourly return observations, absolute entry z 1.5,
normalization z .25, modeled stop 2%, and .25% virtual stop-risk budget.

For each BTC/ETH/SOL candidate, OLS exposure to the equal-weight other two assets
is fitted on bars ending before the observed flow window. Residual volatility,
intercept and anchors are fixed before inference. Source timestamps, raw response
hashes and actual availability remain separate. This is a broad crypto proxy,
not a verified sector hedge. Conditional spot-long sleeves use 10,000 USDT each
for comparison; they are not summed or funded. Max position is 12.5% of that
comparison capital. Short directions are explicitly unsupported.

Four arms share the universe, allowed actions, stop-risk and costs:

- CF_PRICE_ONLY independently ranks the entire universe by absolute residual z.
- CF_SEMANTIC sees current market and observed venue constraints, without memory.
- CF_SEMANTIC_MEMORY uses only matured, audited, actually received prior batches.
- CF_CASH remains cash with zero assumed yield.

The numerical decision and its eligible fresh quote precede model invocation.
Each model arm receives its own actual decision latency and a post-decision book.
A distinct MATCHED track uses the same later book set for information ablation;
its returns are never substituted for the native-time primary results.
Model failure is unavailable, not an invented cash prediction. Cold-start memory
is MEMORY_UNAVAILABLE. The memory alpha claim is activated only before the first
new batch with eligible memory; earlier unavailable arms remain preserved and
are never retrospectively scored. Semantic evidence starts with its own claim.

No successful response is resampled. Explicit failed attempts have separate IDs,
60/300-second backoff and a three-attempt cap. A process interruption after an
external-call intent recovers a durable completed receipt or records uncertain
external state, rather than blindly repeating the call. Source failures retain
raw receipts and do not erase the numerical arm or unrelated growth queue.

## Execution and measurement

Every entry must still have gap room above assumed round-trip costs at a fresh
quote after readiness plus two seconds. Visible depth participation is 1%, impact
limit 50 bps, spread limit 20 bps, fee 10 bps/side and extra slippage 5 bps/side.
Spot lot steps and minimum notional are checked. Funding/borrow/gas are explicitly
not applicable to this unlevered CEX spot simulation. Liquidation bankruptcy
prices are not executable quotes. Exchange queue position, hidden liquidity,
market outages and real fill quality remain unvalidated.

Exit on residual normalization, adverse 2% continuation, or first executable
post-horizon observation. Missing exit liquidity leaves the position open; it
does not manufacture a close or zero loss. Late endpoints beyond the one-hour
grace remain invalid for the original comparison. Stops are polled, not guaranteed
loss bounds. Cash wait and actual exit delay are preserved.

Each actual model attempt is charged an assumed .10 USDT in the research net
return. This is a fixed sensitivity assumption, not a measured provider invoice
or a proven upper bound. Public HTTP/WS use no paid endpoint; existing Codex
subscription use creates no new service commitment. Actual elapsed seconds and
call counts are separate from these assumed information costs. No all-in
profitability claim is justified while actual marginal cost is unknown.

Economic flow exhaustion is separate from price profit: same-symbol/same-side
base quantity in a complete 120-second follow-up window after the common horizon
must be at most half the initial complete-window quantity. Zero initial quantity
or incomplete coverage gives UNKNOWN, not FALSE. This is a venue-window predicate,
not proof that all market constraints ended or caused a price reversal.

## Authoritative evidence and worker

One pinned `authority.sqlite3` holds CASHFLOW/GROWTH budgets, tasks, allocations
and immutable submissions. `authority.json` pins the runtime location and ID;
normal workers reject copied/moved authorities. This is a local integrity measure,
not protection against a privileged person forging a second installation.
No alpha was consumed by the earlier PR #10 diagnostic experiment. New semantic
and memory claims allocate distinct j values in this same registry. Losses,
failures, alpha allocations and prior submissions cannot be removed by restarting.

Each case commits its frozen policy code, input, memory and risk hashes before
the evaluation start. Under `PRESTART_RULE_AND_INPUT_NATIVE_OUTPUT_LATER`, these
are executable-rule commitments, not hashes of AI answers that do not yet exist.
Actual decisions must precede their fills and are independently reconciled later.
The original reference's pre-start-output mode remains available and tested.

Only nonoverlapping whole-market batches count as evidence units. Missing earlier
results block the ordered evidence prefix; unknown is not zero. Raw tails remain
outside clipped utility. The conditional-null assumptions are not established on
this market, so e-values are exploratory diagnostics and never verified edge.
Neither a favorable sample mean nor a synthetic threshold pass promotes a policy.

The worker checks integrity, recovers existing work, manages risk/due outcomes,
then chooses cheap falsification before new research. `ResearchLoop.tick()` claims
work and the real worker acknowledges input/output hashes, duration and costs.
Tasks and source/implementation hashes are frozen. Fourteen days of prospective
collection are configured; no claim that fourteen days have elapsed is made.
After each audited mature batch, successes, failures, costs and rejected choices
become memory, and the next nonoverlapping batch tests the same unchanged rules.
New policy/feature versions require new prospective claims; no automatic real
capital promotion is implemented. VOI priorities are labeled HEURISTIC_PRIORITY.

## Commands

```powershell
python -m pip install -r requirements-alpha.txt
python -m one_trading.alpha_loop --runtime C:\ONE-private\alpha init
python -m one_trading.alpha_loop --runtime C:\ONE-private\alpha tick
python one_trading/alpha_loop/audit.py C:\ONE-private\alpha
.\scripts\install_alpha_loop_task.ps1 -Python C:\Python313\python.exe -Runtime C:\ONE-private\alpha -Activate
Get-ScheduledTaskInfo -TaskName ONE-AlphaLoop-v1-20261029
Start-ScheduledTask -TaskName ONE-AlphaLoop-v1-20261029
Disable-ScheduledTask -TaskName ONE-AlphaLoop-v1-20261029
```

The two-minute Windows task runs as the current limited user with hidden child
windows, separate stdout/stderr and IgnoreNew concurrency. The machine must be
awake and the user logged in. A new source version cannot silently alter a frozen
runtime. Preserve it and register a new cohort using the authoritative registry.
Current CLI intentionally refuses changed source rather than silently migrating.

The review ZIP contains raw prices, stream frames, decisions, source/manifest
hashes, control/market databases, cost records and the standalone stdlib auditor.
Run the auditor directly; it imports no strategy or broker module. Local receipt
times and hashes are not externally notarized timestamps or venue attestations.

```sh
python -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests scripts tools
python -m one_trading.alpha_loop --help
git diff --check
```

The synthetic short-deadline end-to-end fixture is software evidence only. It is
never loaded into real memory or included among actual market outcomes.

## Observed registration defect and preserved replacement

The first actual 120-second window contained no qualifying liquidation. The first
worker incorrectly created a formal economic case anyway. No model call or fill
occurred, and its j=1 alpha allocation and all original records are preserved.
An append-only INVALID_PROTOCOL review isolates that cohort. The repaired worker
records a full-universe numeric screen but requires a nonempty, continuously
observed liquidation window before allocating alpha or registering an economic
case. Quiet windows are observations, not fabricated causal events.

For a new implementation cohort sharing the original authority:

```powershell
python -m one_trading.alpha_loop --runtime C:\ONE-private\alpha-v2 --authority C:\ONE-private\alpha init
```

This pins the new runtime to the existing absolute controller database and global
authority ID. A new claim uses the next global j; old claims, losses, budgets and
submissions are not copied away or refunded. Each cohort keeps its own market
records and frozen code hash. Never use a new standalone authority to restart a
failed experiment's alpha budget.
