# Historical market-data workflow (new experiment, 2026-10-17)

The original recovered v0.3.1 strategy, broker, configuration and 99 tests remain
unchanged. The only modification to an existing module is `research.load_history`:
it opens missing files read-only (no accidental empty DB) and recognizes the new
bulk archive schema. The historical collector, historical panel and experiment
runner are additions in separate modules. **This is a new dataset and experiment,
not reproduction of the excluded original base/stress results.**

## Data sources and boundaries

| Source | Access in this run | Use |
|---|---|---|
| Binance official bulk ZIP archive and S3 directory index | Accessible | All discoverable historical USDT spot pairs, including ended markets; 2018-01 through 2026-09 |
| Binance REST candles | HTTP 451; stopped | Not used; no host rotation |
| Bitstamp official OHLC endpoint | Accessible | BTC/USD, ETH/USD, LTC/USD, XRP/USD, BCH/USD reference series; 2018-01 through 2026-09 |
| Coinbase official Exchange candles | HTTP 403; stopped | Not collected |
| Kraken official full-history download checksum URL | HTTP 403; stopped | Not collected |
| Bitfinex official public candles | HTTP 403; stopped | Not collected |

The Binance bulk archive is a separately published downloadable dataset, not an
alternate REST API host. No account, credentials, real orders or withdrawal
capability is introduced. The collector stops on access/rate denial, does not
follow redirects or rotate hosts, and respects the environment's network proxy.
It reuses TLS connections to avoid repeatedly establishing the same connection.

Binance's archive index is broader than today's exchangeInfo, but **not the entire
crypto market or a certified historical listing register**. Missing/removed archive
objects, non-USDT pairs, other venues, DEX-only assets, extinct venues and token
identity changes still limit coverage. A first observed daily bucket in January 2018 is
left-censored, not proof that a pair listed then. A last observed daily bucket is not by
itself proof of delisting. Current market-cap ranks are not used.

`market_events` distinguishes observed first/last daily buckets from manually reviewed
listing/delisting notices. The small notice seed is stored with its source URL,
review time and conservative publication availability bound. It is not a complete
announcement history and is not used to improve strategy performance. Ticker reuse
at the 2022-05-31 LUNA transition is an explicit identity boundary; features/labels
never bridge it. All other token migrations still need identity reconciliation.

Bitstamp OHLCV has base volume but no quote volume. Its `quote_volume` is NULL,
never fabricated as `close × volume`. These five reference pairs do not constitute
a survivorship-free Bitstamp trading universe. USD/USDT and venue basis differences
are reported as differences, not automatically classified as corrupt prices.

Primary source specifications:

- <https://github.com/binance/binance-public-data>: daily/monthly ZIPs, checksums,
  and the change to microsecond spot timestamps from January 2025.
- <https://www.bitstamp.net/api/>: OHLC schema, 1,000-bar cap, end parameter priority.
- <https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles>
- <https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data>
- <https://docs.bitfinex.com/reference/rest-public-candles>
- <https://www.binance.com/en/support/announcement/detail/f73b083ba6834771b07dbe5319917ae5>
- <https://www.binance.com/en/support/announcement/detail/836af23f13f24be08d99ec157ed1edac>
- <https://www.binance.com/en/support/announcement/detail/d044a6742e484b77a170111460b0eed3>
  (LUNA identity warning: historical indexed notice; current direct page redirects).

## Reproduce

Linux / Python 3.10+ standard library only; no added dependency. Collection uses a POSIX single-writer lock. Run from the repository
root. Keep the exact run's dataset and source manifests for strict reproducibility:
upstream archive files can be revised. Fetching fresh bytes in the future is not
necessarily the same experiment.

```sh
python -m unittest discover -s tests -v
python -m one_trading.historical_data \
  --db output/market/market.sqlite3 --cache output/market/cache \
  --start 2018-01-01 --end 2026-10-01 --workers 16 --memory-build
python -m one_trading.reference_data --output output/reference \
  --start 2018-01-01 --end 2026-10-01
python -m one_trading.walkforward \
  --archive output/market/market.sqlite3 --output output/wf-zero \
  --start 2019-01-01 --end 2026-10-01 --missing zero
```

`--end` is exclusive. Monthly collection requires month boundaries. Resuming the
same collector uses verified cached bytes and rejects a changed collection spec.
Missing files or invalid records leave `collection_complete=false`; the loader
refuses incomplete collections. HTTP/data failures are logged, never silently
turned into successful coverage. A new replay output directory is required;
failed runs must not be overwritten by successful ones.

For a diagnostic run that refuses any unobservable held market, use a different
output directory and `--missing fail` (the default). A failed diagnostic is not a
performance result. `python one.py replay` can read the new archive through the
compatibility dispatch, but keeps its old universe/gap behavior; use the new
walkforward runner for the historical-universe protocol. No claim of execution
parity is made between these runners.

## Database and provenance

- `snapshots`: existing Store schema, with original ZIP/index/checksum/native JSON
  bytes, SHA-256, exact public request URL, actual retrieval time. No manufactured
  exchange `/time` response and no derived CSV mislabeled as an HTTP response.
- `sources`: links normalized records to the original raw snapshot.
- `candles`: venue/pair/open timestamp primary key, OHLC, actual close timestamp,
  base/quote volume, source id. Duplicates must agree exactly.
- `market_events`: observed boundaries and separately qualified official notices.
- `archive_meta`: frozen collection specification and completeness status.

The collector verifies each Binance ZIP against the publisher's `.CHECKSUM`.
Milliseconds and microseconds normalize to milliseconds. Daily bars shortened by
suspension/delisting preserve their actual end timestamp. No gap candles are
interpolated. Invalid OHLC/negative-volume source rows are retained in
`rejected_candles` and excluded from features; no numbers are repaired. Before replay, all raw hashes are verified and **every normalized
candle is regenerated from the original ZIP and compared with the database**.
The source manifest and canonical normalized-candle digest are independent of
thread completion order and SQLite page layout. A local hash alone cannot prove
historical publication time or source authenticity.

## Chronology and evaluation

- 2018 supplies warmup; fixed test start is 2019-01-01. No asset becomes a candidate
  before 240 consecutive observed daily bars. Gaps and known identity boundaries
  reset the lookback. Stable/fiat bases and explicitly named leveraged tokens are
  excluded. These classification exclusions are not an exhaustive asset ontology.
- At each completed signal day, the universe is rebuilt from the trailing 30-day
  mean quote volume. Only then are the top 40 candidates selected. No current
  liquidity ranking, current active-status list, or future return is used.
- The recovered 14-feature Ridge model, 28-day target, 540-day rolling training,
  seven-day refit, risk rules, completed week/month features and D+2 execution
  delay are retained. This is daily evaluation using weekly/monthly context,
  **not a newly optimized weekly-only strategy**.
- Labels that encounter a missing/nontrading day after entry receive the model's
  clipped full-loss target. They are not dropped as if only survivors mattered.
  If no entry was possible, no imaginary trade label is generated.
- In `zero` mode a held missing/nontrading market is written off after the first
  missing UTC day. This is a deliberately pessimistic scenario, not evidence of
  zero legal recovery or a fill at the last historical price. The day is quarantined
  from new execution. Real liquidation, pauses, reopening and recoveries need
  timestamped notices and intraday data. Shortened terminal candles may still have
  actual open/stop executions under the inherited daily-bar convention.
- Continuous account results retain the permanent drawdown halt. Independent yearly
  folds reset virtual capital/risk state and warm the model only on preceding data.
  The annual returns are **not stitched** into a supposed continuously tradable
  result. Later years can expose weaknesses hidden by an early permanent halt.
- Base fees/slippage are 10/10 bps per side; stress is 20/30 bps per side. Cases
  retrain and re-execute because cost enters the label and position sizing. Their
  difference is not a pure fee deduction from an identical trade list. Fees and
  assumed slippage paid are also reported directly for each realized path.
- BTC buys at the same test-start open and liquidates at the same final close with
  the same cost rates. Returns, drawdowns and cash are denominated in USDT units.
  Daily-close maximum drawdown does not capture every possible intraday trough.
- BTC regimes are classified from **prior-day** SMA200 and 28-day momentum, before
  measuring that day's return. Regime returns compound disjoint classified daily
  returns; they are attribution, not separately tradable sub-strategies.
- Parameters were fixed before inspecting these performance results. This does not
  make already-known historical regimes untouched out-of-sample evidence.

## What the price model does and does not test

| Component | Implemented and testable here | Still absent |
|---|---|---|
| Price context | Base width, contraction, relative strength, completed weekly/monthly returns | Evidence that a price base predicts business revaluation |
| Learning | Chronological matured labels and repeated forward prediction | Calibrated probabilities or proven positive expected return |
| Universe | Historical archive presence and trailing liquidity | Whole-market historical membership; exhaustive exchange/identity/event coverage |
| Trading | Delayed simulated fills, fees, slippage, risk limits | Depth, spread history, partial fills, venue filters, transfers, taxes, live execution |
| Revaluation | None; no fabricated proxy for missing inputs | Usage, revenue, token value capture, supply/unlocks, contracts, incentives, catalysts |
| Fundamental timing | Requirement documented | Point-in-time panel with publication, collection, effective and revision times |

A price-model failure rejects that implementation under the tested protocol. It
neither validates nor falsifies a yet-unimplemented fundamental revaluation engine.
Do not promote this system to funded trading on the basis of offline test counts.

Memory-build mode reconstructs the database from the frozen HTTP cache in RAM,
checks SQLite integrity, and atomically exports a standalone file. It requires a
new output database path; it never overwrites a prior dataset. This mode was used
after two file-backed builds failed database integrity checks. Those failed
builds were rejected before any performance calculation. The precise underlying
filesystem cause was not established. Avoid opening a writable collection DB
from another process while it is being built.
