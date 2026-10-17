# ONE real-data walk-forward results — 2026-10-17

New-data historical experiment. **Not reproduction of the missing original base/stress runs.**
**Verdict: reject the current policy as ONE's capital-growth engine.** The base
continuous account gained 11.87% over approximately 7.75 years, versus 2,150.33%
for BTC, while investing only 0.77% of equity on an average day. Its 3.46% maximum
drawdown mainly describes near-cash exposure, not demonstrated ability to capture
major revaluation cycles. The strategy made no trades in 2023. Fundamental
revaluation inputs remain unimplemented, so their potential is not tested here.

No parameter search or performance-driven tuning was conducted in this run.

Data: 756 Binance USDT spot histories, 844,092 daily candles, 28,228 monthly source ZIPs. Collection: 2018-01-01 through 2026-09-30 UTC.
Shortened daily bars retained: 297; corrupt source rows quarantined: 7; histories with internal gaps: 26.
Bitstamp: five fixed major USD pairs, 15,975 reference OHLCV bars; not a complete historical universe.
Source bytes, publisher checksums, normalized-row regeneration and SQLite integrity were verified before replay.

## Continuous account: 2019-01-01 through 2026-09-30 UTC

| Case | Strategy net return | Strategy MDD | BTC net return | BTC MDD | Fees | Slippage | Fills | Avg exposure | Halt latched |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| base | 11.87% | -3.46% | 2150.33% | -76.63% | 63.65 | 63.65 | 334 | 0.77% | False |
| stress | 10.06% | -3.21% | 2136.87% | -76.63% | 84.22 | 126.34 | 252 | 0.52% | False |

Initial virtual capital: 10,000 USDT. MDD is daily-close marked-equity drawdown. Fees/slippage amounts are USDT. Base = 10/10 bps each side; stress = 20/30 bps each side. Cost cases retrain/re-execute; they are not the same trades with a fee subtracted.

## Independent yearly folds

Each year starts with fresh virtual capital/risk state and preceding-data model warmup. Do not compound this table into a supposed continuous investable result.

| Year | Base return | Base MDD | Base BTC return | Stress return | Stress MDD | Base fills | Base avg exposure |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2019 | 1.25% | -0.67% | 93.62% | 0.98% | -0.87% | 19 | 0.29% |
| 2020 | 2.79% | -1.43% | 300.38% | 3.65% | -0.80% | 81 | 1.49% |
| 2021 | -2.17% | -3.05% | 59.15% | -1.81% | -2.15% | 71 | 0.86% |
| 2022 | 0.91% | -0.35% | -64.35% | 1.16% | -0.29% | 18 | 0.24% |
| 2023 | 0.00% | 0.00% | 154.60% | 0.00% | 0.00% | 0 | 0.00% |
| 2024 | 10.26% | -1.97% | 120.42% | 6.54% | -1.73% | 93 | 1.91% |
| 2025 | -0.59% | -0.75% | -6.71% | 0.04% | -0.12% | 27 | 0.63% |
| 2026 | 0.12% | -0.50% | -4.97% | -0.23% | -0.32% | 14 | 0.57% |

2026 ends on September 30. Full metrics, including each cost case’s BTC benchmark and costs, are in `comparison.json`.

## Prior-day BTC regime attribution (continuous base account)

| Regime | Days | Strategy return on classified days | BTC return on classified days |
|---|---:|---:|---:|
| BEAR | 723 | -0.00% | 40.41% |
| TRANSITION | 977 | 2.17% | -60.05% |
| BULL | 1130 | 9.51% | 3912.04% |

Regimes use the prior close versus SMA200 and 28-day momentum. Returns compound disjoint daily observations; they are attribution, not separately executable strategies.

## Interpretation and limitations

- The recovered price-only policy is evaluated under a new historical-liquidity universe and documented missing-market assumptions. The original strategy/broker thresholds were retained.
- This run does not establish profitable autonomous trading, whole-market coverage, or untouched out-of-sample alpha. Archive availability, token identity, trade feasibility and liquidity assumptions remain limitations.
- Full fundamental revaluation analysis is absent: usage, revenue, token value capture, unlocks, publication-timed catalysts and revisions are not connected.
- Listing coverage consists of observed daily-bucket boundaries plus a small curated official-notice seed. These are not an exhaustive cross-exchange listing/delisting register.
- Missing held markets use an explicitly pessimistic full-loss writeoff with no fabricated sale. See writeoff counts in the machine-readable comparison and the workflow for the alternative fail mode.
- Two file-backed dataset builds failed SQLite integrity checks and were rejected before performance calculation. The final archive was rebuilt from frozen source bytes in memory and atomically exported; the underlying filesystem cause was not established.
- All original and new test results must be distinguished from these real-market-data simulation outputs. No real orders were sent.

## Reproduce and audit

See [workflow](../../HISTORICAL_WORKFLOW.md), `protocol.json`, `dataset_summary.json`, `coverage.csv` and `comparison.json`.
Normalized candle SHA-256: `b139383a4eacb5cf9150354576eb9cf2f44937c9a3e019bacfa88f3e37d2b98f`.
Source-manifest SHA-256: `810d1b1265dbb9531df2b6b89718c1a106efafcdde858eb6a6924b078c0228bf`.
Frozen raw bytes and full runtime artifacts are retained separately. Collected prices and individual fills/model records are deliberately not committed to this repository.

Validation: 120 local tests passed (99 recovered, 21 new). A fresh-process 2024
base fold using strict missing-data failure matched the recorded fold exactly,
including equity, fills, model audits and summaries. All reported main runs had
zero missing-market writeoffs; this does not validate recovery assumptions for
future holdings that do encounter a disappearance.
