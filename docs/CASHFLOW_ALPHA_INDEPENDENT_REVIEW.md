# Cashflow Alpha v1 — independent review

**Decision: REJECTED.** The frozen policy fails its research profitability gates
and the cashflow objective. Software-accounting and chronology checks pass; those
checks do not convert a failed strategy into a profitable one. No live approval is
given.

The review independently reconstructs campaigns from raw BUY/SELL quantities,
prices and fees, rather than using the strategy's campaign or metric functions.
The analyzed continuous account runs from 2019-01-01 through 2026-09-30 UTC.
Returns are in USDT units, net of modeled execution costs, before tax and FX.

| Independently recomputed metric | Base | Stress |
|---|---:|---:|
| Total net return | +1.6266% | -0.4628% |
| CAGR | +0.2085% | -0.0599% |
| Daily-close maximum drawdown | -11.7961% | -12.3236% |
| Average daily capital exposure | 2.6661% | 2.6495% |
| Closed campaigns | 113 | 113 |
| Win rate | 30.9735% | 30.9735% |
| Average winning / average losing campaign | 2.3832 | 2.1867 |
| Profit factor | 1.0694 | 0.9812 |
| Median monthly net return | 0.0000% | 0.0000% |
| Positive months | 12/93 | 11/93 |
| Months returning at least 25% | 0/93 | 0/93 |

Base costs are 10 bps fee plus 10 bps slippage per side. Stress costs are 20 bps
plus 30 bps per side. Cost cases re-execute their own paths; their difference is
not merely a subtraction from an identical fill list. Campaigns include adds and
partial exits until inventory returns to zero. Exposure is a daily marking
measure, not intraday time-weighted exposure; the last day uses the explicitly
retained exposure before end-of-test liquidation.

## Independent checks

- All 18 accounts (two continuous, sixteen independent calendar folds) reconcile
  cash, inventory, campaign PnL, fees and reported performance metrics.
- The two continuous accounts each have 405 weekly plans. Every BUY is associated
  with a prior Sunday signal and Tuesday execution, and each recorded feature's
  latest input predates observation.
- Historical liquidity ranking and eligibility were recomputed separately from
  the strategy's panel cache for all 405 dates in each continuous account.
- Physically removing all data on and after 2023-01-01 and rerunning produces
  identical prior equity, fills, writeoffs and plans: 1,461 days and 61 fills per
  cost case. This is a real-data prefix-invariance check, not just a synthetic test.
- Every actual fill has a corresponding source candle. No continuous-account fill
  occurs on a zero-volume daily candle. Neither continuous account records a
  missing-market writeoff.
- Five independent audit regression tests cover ledger errors, partial exits and
  adds, writeoffs, year boundaries, timestamp violations and bootstrap determinism.
  These tests establish software behavior only.

An additional raw-data scan found that non-leveraged opening-price discontinuities
above 5x or below 0.2x followed observed gaps. These include BNX, BTCST, COCOS,
DREP, LUNA, QUICK, STRAX, SUN, VEN and VIDT. The 240-consecutive-day history rule
resets at these gaps, so these specific discontinuities are not spliced into
feature windows. This does not certify complete token-identity reconciliation.
No symbol was removed after inspecting performance.

## Uncertainty and interpretation

A deterministic 4,000-replication circular bootstrap of 93 monthly returns uses
three-month blocks. Its historical 95% interval for annualized geometric return
is **[-2.12%, +3.03%]** for base and **[-2.41%, +2.70%]** for stress. Both include
zero. These intervals describe resampling of the observed history; they are not
calibrated future-return bounds or the probability the strategy will succeed.
Longer regime changes, the prior research selection process and future adaptations
are not represented.

The policy is mostly inactive and its thin gross edge disappears under higher
costs. Of 26,940 weekly universe-symbol observations, only 180 qualify as entry
candidates (0.668%). Eighty SELL fills are stop-first exits. This supports testing
the excessive conjunction of entry filters and entry/stop timing as *new*
hypotheses; it does not justify increasing leverage on this version.

The prior data and historical regimes were already used in research. The present
chronological replay is not untouched out-of-sample evidence. It also lacks
publication-timed fundamentals, actual order-book fills, funding/liquidation
simulation and forward operational evidence. Funding is zero only because this
version holds unborrowed spot. Daily-close 3%/15% halt logic does not guarantee
intraday losses stay within those values.

## Reproduce the independent audit

After running the frozen experiment:

```sh
python scripts/validate_cashflow_alpha.py \
  output/cashflow-v1/base_continuous.json \
  output/cashflow-v1/stress_continuous.json \
  --archive /path/to/market.sqlite3 \
  --prefix-date 2023-01-01 \
  --output output/cashflow-v1/independent_continuous_audit.json
python scripts/validate_cashflow_alpha.py \
  output/cashflow-v1/base_20*.json output/cashflow-v1/stress_20*.json \
  --output output/cashflow-v1/independent_fold_audit.json
```

Retain the exact dataset, frozen protocol, code hashes and raw experiment outputs.
The public repository includes code and summarized review evidence, not collected
prices or individual runtime ledgers.
