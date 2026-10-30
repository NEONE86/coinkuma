# ONE observer: local vertical slice

Base: PR16 `18d0768c934f3bca1d1be70f17e5f643929f7e0b`. This branch adds
`one_sentinel/` and does not change the existing Alpha/batch/pilot source or
runtime. Receipt recovery is on a separate R branch. No remote PR, scheduler,
VM, model subscription, live account or channel has been activated.

## Fixed contract and acceptance

The policy/account JSON files are frozen into each new ledger with their hashes
and the exact observer Python source hash. A changed contract/source requires a
new runtime; the old experiment is never revised. The one PAPER account starts
at 10,000 USDT. This is a ledger denomination, not deposited money.

The first hypothesis is that **observed restoration of official spot access**
can permit subsequent participation demand. First-seen metadata is only a
baseline. An unobserved prior halt/new listing is not inferred. Official access
is a fact; participation demand is an inference; positive seven-day net return
is an unvalidated assumption. This is not adoption of the previously rejected
absorption/delay strategies. All thresholds are frozen engineering assumptions,
not fitted returns. Source metadata supplies spot permission and LOT_SIZE /
minimum notional; unsupported instruments cannot enter.

| Action | Observable rule / quantity |
| --- | --- |
| ENTER | Known non-trading/forbidden spot → trading+spot; fresh price, metadata and closed 1m volume ≥10,000 USDT; initial 1,000 USDT before fee |
| NO_ACTION | Ineligible/consumed thesis, insufficient data or account constraint; reason and deterministic/AI review states stay distinct |
| HOLD | Existing valid position; on gaps retain inventory, freeze buys and create RISK; model disabled is not an inventory write-off |
| ADD | Once per position, same fresh thesis, net marked return ≥2%, never after reduction; 500 USDT before fee |
| REDUCE | Once for net marked drawdown ≤−3%, or repeatedly while marked concentration/exposure is over its limit; half quantity rounded down to the instrument step |
| EXIT | Official access invalidation, net marked drawdown ≤−5%, or seven days after the initial fill; all remaining quantity |

EXIT/REDUCE cancel pending buys. One unresolved action per symbol, transactional
cash reservations and cost/marked exposure caps prevent duplicate commitments.
Buy reservation includes 0.1% fee. A subsequent fresh book after at least 2 seconds
uses ask for buys/bid for sells, adverse 5 bps slippage, 0.1% fee, ≤20 bps spread
and ≤10% of top-level displayed depth. These are PAPER assumptions, not evidence
of achievable fills. Expiry releases the reservation via an appended rejection.
Partial sells remove proportional acquisition cost; missing books do not close
positions. Minimum filters may leave unfilled dust, reported as rejection and
retained inventory. Account configuration defines the risk priority and ceilings.

## Data and basic alerts

Broad: one Binance `!miniTicker@arr` stream, changed symbols only. Its v/q values
are rolling 24h totals, never exact minute volume. Deep: at most five genuine
closed 1m kline streams. Priority is OPEN inventory, structural event, minimum
residency, price mover, initial list, then lexical ties. Residency is 300s;
unheld candidates expire after six hours. Gaps, delistings and unsupported fields
are explicit. There is no whole-market OI or cross-exchange coverage.
See [official stream definitions](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams).

Official structure is GET `/api/v3/exchangeInfo`, every 300s: 288 planned calls/day,
150s expected detection wait under uniform arrivals. Deep books are requested
only for ready PAPER actions, max400/day. The bounded adapter stops at688 HTTP
attempts/rolling24h; the other312 of the proposed1000 cap are reserved, not
automatically consumed. Retries/errors count. Separate approval is required for
this new observer budget; it never touches or resets existing Alpha authority.
Slow economic adapters remain disabled; the older eight-source 15min proposal
would still have 450s mean polling delay, not instantaneous discovery.
The [exchange metadata endpoint](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints)
is evidence about trading availability, not a forecast source.

BASIC_EVENT/RISK outbox rows are deterministic and committed with the event,
without a model call. Candidate merging uses event+category identity. Six analysis
attempts/rolling24h are reserved 2/2/2 for holdings/new/structural; failures count,
queue cap32, expiry30min, age priority in5min bands. Disabled, expired, timeout,
queue-full and budget-blocked are UNREVIEWED, never an AI NO_ACTION.
The model integration is an offline contract only: real paid inference remains
disabled. Synthetic AI answers add an AI_ANALYSIS revision without rewriting the
first alert or submitting orders.

The HTTPS webhook adapter requires an exact approved URL hash and trial manifest.
No URL or token is stored in the public ledger. Timeout after send is UNKNOWN and
requires reconciliation; it is not blindly retried. A durable provider receipt
is recovered without another send. Provider ACK and phone receipt are distinct.
The 60s target is valid-receipt→ACK, not publication→receipt or a guarantee.
Only synthetic ACK timing has been tested here. A real receiving channel has not
been selected or approved. Synthetic demo uses a fake provider only.

## Reproduction

Run in this checkout with Python 3.12+; all commands below are offline:

```sh
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s tests -p "test_sentinel*.py" -v
python -B -m one_sentinel.demo --out ../sentinel-demo-input
python -B -m one_sentinel --runtime ../sentinel-demo-runtime replay --input ../sentinel-demo-input/part1.jsonl
python -B -m one_sentinel --runtime ../sentinel-demo-runtime replay --input ../sentinel-demo-input/part2.jsonl
python -B -m one_sentinel --runtime ../sentinel-demo-runtime replay --input ../sentinel-demo-input/inputs.jsonl
python -B -m one_sentinel --runtime ../sentinel-demo-runtime export --out ../sentinel-demo-export
```

Copy `../sentinel-demo-runtime/raw/` into `../sentinel-demo-export/raw/`, then:

```sh
python -B scripts/verify_sentinel.py ../sentinel-demo-export
```

The independent verifier imports no project implementation. It rechecks raw
hashes/parsed facts, event/decision/book links, cutoff/delay, price, quantity,
fee, cash, inventory and realized PnL. A rehashed wrong fee is rejected.
The demo intentionally loses money. It has one opened/closed position and three
virtual fills (entry, reduction, exit), not three independent experiments.
The maturity horizon remains pending and ineligible for an accuracy score.
Separate processes exercise restart; full duplicate replay adds no fill.

The review ZIP contains a separate REAL_REUSED runtime with three unchanged
previously received exchangeInfo documents. This is metadata ingestion only:
zero new structural events, zero new market requests and zero forward trades.
Do not treat replay processing time as the original information receipt time.
Synthetic and real classifications cannot share a runtime.

## Approval and remaining operating evidence

R: local recovery implementation/tests complete; no actual binding or repair.
O: local raw/feature/membership and bounded public adapter implemented.
D: fixed deterministic policy/PAPER account and analysis scheduling contract
implemented; paid model reasoning remains disabled.
E: local outbox, fake delivery, bounded approved-webhook adapter and independent
account verification implemented. O/D/E local integration is tested. **The actual
market + approved-channel MVP and 48h acceptance are not complete.** Remote PR
publication is a separate permission. No CI result on an unpublished commit is
claimed; the existing local full suite and CLI checks were run.

A first actual smoke needs explicit approval for a new isolated bounded public
data run, and a user-owned HTTPS JSON receiving channel approved for transmission.
Keep the channel URL in local `ONE_OBSERVER_WEBHOOK_URL`, not chat or the ZIP.
The approval file must bind policy_hash, account_hash, source_hash,
approval_reference, run_id, approved_runtime (absolute canonical runtime path),
start_at, stop_at (≤48h), public_data=true,
model_cash_budget=0, live_orders=false, and channel_url_sha256 if delivery is
approved. `python -m one_sentinel --runtime NEW smoke --approval FILE --seconds 300`
is the later manually invoked entry point; it is not scheduled now. It uses the
installed `websockets` package only for actual smoke; offline tests use stdlib.
Creating a manifest is not a substitute for human approval.

PR #17 adds a persistent approval claim at the operating account's fixed
`~/.one-observer/approval-claims.sqlite3`, independent of runtime directories and
code checkouts. It must not be deleted or copied to reset budgets. Offline tests
substitute a temporary store and never change that actual account state.
A legacy approval without an explicit target
is rejected by the new code. Resuming the same run retains its ledger instance
and consumed attempts. This local guard is not cryptographic authorization
against someone who can edit both the approval and all filesystem records.

First run5min before seeking48h acceptance: verify endpoint accessibility, clock
skew, bounded errors, provider ACK and manually confirmed phone receipt. Long-run
storage/CPU capacity, WS reconnect coverage and real transport behavior are not
yet validated. The current audit-first ledger queries are not claimed to scale
indefinitely; measure/optimize projections before48h. Formal unattended operation
also requires host-external last_seen, missing-host alert and recovery receipt;
no external watchdog exists yet. A48h trial is not that approval.

No automatic raw deletion is implemented. Pins retain both structural revisions
and all run raw bytes while a thesis/position is open or unaudited;30days is not
a retention limit. Review/closure/horizon must all permit any later deletion.
