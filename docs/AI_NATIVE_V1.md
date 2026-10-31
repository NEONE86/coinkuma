# ONE AI-Native Decision Lab v1 — implementation and evidence contract

## One-line objective

Grow the owner's **net, survivable capital** with an autonomous, always-improving
decision system; data collection, explainable decisions and audit are instruments,
**not** the objective. No implementation is evidence of a trading edge until it
outperforms independent, deployable comparators **after execution costs**.

## Delivered in this additive revision

`one_trading.ai_native` implements one finite, local, isolated PAPER replay:

1. Receive timestamped, frozen multi-symbol best bid/ask + *visible* quote size.
   Reject duplicate, backdated, future-quote, incomplete or changing-universe
   packets. These are supplied research files; the module does **not** access
   Binance, connect to an exchange, collect new data or run on a schedule.
2. Give every arm **its own** cash, positions, pending intent and previously
   matured learning memory. CASH, price momentum, price reversal and an online
   memory-adapting comparator run separately. They are not jointly invested.
3. Optionally let Codex choose freely among `BUY`, `SELL`, `HOLD`, the symbol,
   desired notional fraction and falsifiable hypothesis. This is **not** a fixed
   entry strategy masquerading as an AI. `SELL` is a spot exit, not a short.
   Optional model access is **off by default** and requires explicit bounded
   call authorization and an existing private model runtime directory.
4. Keep intent and decision identity. A decision can fill only against a **later
   received** fresh quote after measured model time plus execution delay; charge
   explicit spread, taker fee and modeled slippage. Check visible size and the
   maximum research depth participation. Rejected requests do not fake fills.
5. Wait for the independent horizon to elapse, mark counterfactual outcomes,
   and expose **only already matured** episodes to subsequent decisions. Label
   markouts are not the actual realized trading P&L.
6. Preserve a hash-chained ledger with source packet content, time, decisions,
   eligible orders, paper fills, rejected attempts, mature labels and final
   separately costed accounts. An independent `audit.py` (without importing the
   experiment) recalculates chronology, price, depth, fee, P&L and maturity.

There is **no real order router, real wallet, live notification, scheduler,
portfolio mutation or automatic retry** in this module. The Observer and sealed
trial remain unchanged. No live economic advantage is claimed.

### PR #18 review corrections (offline only)

- **Settlement completeness:** independent verification now requires each order
  that is eligible at a received quote to produce exactly one fill or justified
  rejection *on that receipt*. Deleting a losing fill/rejection and rehashing
  the ledger cannot leave a fictitious pending order that passes the audit.
  Orders not yet eligible, including orders created on the final receipt, may
  legitimately remain pending.
- **Replay-stable model timing:** a successful Codex decision now carries its
  original recorded decision latency into replay from a private cache. A cache
  created by an older version without that evidence fails closed; create a
  separate private model runtime for a new bounded attempt, preserving the old
  artifact for investigation. No earlier PAPER fill is inferred from cache-read
  speed. Algorithmic control arms still use measured local proposal duration.
- **Current-frame risk:** mark existing positions at the current receipt's bid
  and latch any drawdown halt **before** settling a pending BUY, then recheck
  equity after settlement costs. A drawdown halt does not obstruct permitted
  SELL exits.

These are verified with deliberately forged ledgers, a delayed mocked model
response, and a two-asset price-shock test. They are software integrity evidence,
not a market-return result.

## Minimal reproducible exercise

From the checkout root:

```bash
python -m unittest discover -s tests -p test_ai_native.py -v
mkdir -p /tmp/one-ai-native-private
python -m one_trading.ai_native run \
  --input tests/fixtures/ai_native_synthetic_quotes.jsonl \
  --output /tmp/one-ai-native-private/synthetic_result.json \
  --data-use SYNTHETIC
python -m one_trading.ai_native verify \
  --input /tmp/one-ai-native-private/synthetic_result.json
```

The target file **must not already exist**: replay never overwrites private
evidence. The built-in fixture is intentionally **invented** and does not
establish any market, strategy or model result. Only CASH and algorithmic
controls are exercised by the default command: `AI_FREE_DECISION` is absent,
not silently presented as if an LLM had run.

To authorize a **separate**, locally bounded model experiment in an environment
that already has an authenticated Codex binary and an existing directory:

```bash
python -m one_trading.ai_native run \
  --input /path/to/frozen_public_quote_packets.jsonl \
  --output /path/to/private/new_result.json \
  --data-use PROSPECTIVE_REPLAY \
  --ai-mode codex \
  --private-model-runtime /path/to/preexisting_private_model_runtime \
  --max-ai-calls 12 \
  --model gpt-6-astra
```

On a Windows scheduler where `codex` is missing from the child process PATH,
specify `--model-executable` with the **already installed, verified** absolute
Codex executable path (no installation or PATH mutation by the experiment).

The latter command is a **future opt-in example**, not an executed instruction
and not authorization to start collecting data. Calls incur provider usage; no
model inference was executed by this revision's no-model offline checks.
Unknown provider cost is explicitly `UNKNOWN_NOT_ZERO` in private per-call
artifacts. The historical-data mode is never prospective forward evidence.

### Frozen packet schema

One JSON object per line, unique ascending `received_at`:

```json
{"id":"one","received_at":"2026-10-30T00:00:00+00:00","quotes":{"BTCUSDT":{"bid":100.0,"ask":100.1,"bid_size":50000,"ask_size":50000,"exchange_at":"2026-10-30T00:00:00+00:00"}}}
```

Fields are research-standardized quote observations, **not** direct Binance
execution guarantees. `received_at` is the supplied source receipt time,
`exchange_at` is supplied exchange event time. The experiment verifies internal
chronology but cannot authenticate externally supplied timestamps by itself.
The embedded receipt hash is for canonical structured content, **not** a claim
that the source socket's exact wire bytes were recovered.

No migration path is fabricated from the previous 285-second trial's mini ticker
observations: it contained **zero Book receipts** and no policy event, so it
cannot establish visible-depth fills, realized opportunity, decision latency or
model performance. Those 315 receipts are a *transport* validation set, not
this project's PAPER trading sample.

## Fixed initial research parameters (not validated optimum)

| Property | Frozen default | Meaning |
|---|---:|---|
| Separate CASH per arm | 10,000 USDT | Simulated comparison only |
| Execution delay | 2 seconds + measured model latency | Later receipt required |
| Quote max age | 30 seconds | Stale book unusable for PAPER fill |
| Spread cap | 20 basis points | Do not simulate impossible crossing |
| Fee / slippage | 10 / 5 basis points per side | Cost assumption; stress later |
| Visible-size participation | 1% | Conservative single-level depth cap |
| Single spot position | ≤12.5% of equity | Research account bound |
| Total exposure | ≤40% of equity | Research account bound |
| Equity drawdown halt | 15% | Freeze new buys |
| Independent learning horizon | 1 hour | Never deliver unmatured feedback |

All defaults are engineering assumptions, not real order fill observations.
Prices and returns in the tiny fixture have no predictive value.

## Economic score: the only promotion standard

At every promotion decision, compare the proposed AI arm against CASH, simple
controls, fees/slippage stress and **untouched forward periods**:

- `net_equity`, `net_return`, `realized_pnl`, open position bid mark, fees,
  modeled slippage, turnover and max observed drawdown;
- number of eligible decisions, filled/rejected trades, missing model responses,
  open orders, independent matured episodes and how long feedback is delayed;
- whether net gains persist **after** latency, size, spread and adverse-fill
  assumptions, not only on midprice markouts or internal expected scores;
- risk-adjusted growth relative to CASH and deployable controls, across regimes;
- replay/model inference costs deducted from actual business capital economics.

Do not report a high backtest return without chronology, out-of-sample selection
penalties, and cost capacity. No P&L from a synthetic fixture is investment
performance. A cashflow target of 5–10 million KRW/month on 20 million KRW
starting capital would require roughly 25–50% monthly growth, an **aspiration,
not a performance projection**.

## Next versions, prioritized by impact on genuine P&L

1. **Trusted forward input:** explicitly approved 24/7 public spot/perpetual
   data capture with exact source/time, level-2 freshness, order-flow imbalance,
   OI, funding, liquidations and cost/availability envelopes. Add authenticated
   news announcement times and flow traces only where provenance permits; do
   not backfill these as if known sooner. A separate approval controls new
   network collection, schedule, and their cost ceilings.
2. **AI expert competition:** concurrent, independent candidate models for
   capital-flow detection, pre-event anticipation, trend formation, breakdown,
   forced-position exit and regime transition. Their free-form forecasts and
   portfolio actions are benchmarked on the same frozen opportunities, with
   capital allocated only to models that hold up on untouched data.
3. **Learning loop:** update beliefs *after* horizons mature; track calibration,
   lead-time to first price move, regime degradation and per-expert economic
   contribution. Advance/challenge/retire models with explicit evidence; never
   auto-train or rewrite an active decision policy on an uncleared sample.
4. **Provisional live PAPER:** wire new policy events to new Book receipts,
   coherent decision-latency accounting and conditional PAPER fills. Keep
   missed opportunities in the denominator. Carry forward account state and
   calculate costs *as experienced*, not on hindsight prices.
5. **Capital system:** only after positive net prospective evidence, independently
   consider a capped micro-capital experiment and then progressive scaling;
   keep execution permissions physically separate from model permissions.

### Stop / continue

- **Passed now:** offline software chronology, bounded conditional PAPER
  accounting, independent recalculation, missing-model honesty, control arms.
- **Not yet passed:** actual LLM inference, complete order-book capture,
  liquidations/news/flow alpha, positive economic edge, 24/7 resilience,
  production execution.
- **No-go alternatives:** directly trading on a signal's own quote, presenting
  mini-tickers as Book quotes, scoring an unmatured episode, optimizing only
  retrospective profits, or conflating separate paper sleeves with a single
  deployable fund.
