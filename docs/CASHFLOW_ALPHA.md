# Cashflow Alpha v1: first early-revaluation price-proxy experiment

This branch extends PR #2 at `f9b26daad87167e7ebee45c68000dac9bea57500`.
The original main, recovery branch, Ridge strategy and broker are preserved.
There is no exchange order adapter, deployment, leverage approval or live-account access.

## Reuse and replacement

| Reused | Replaced / rejected | Still absent |
|---|---|---|
| Official historical collector, raw response snapshots and publisher checksums | Ridge entry predictions and the old +11.87% low-exposure policy | Publication-timed usage, revenue, supply/unlocks and value-capture evidence |
| Historical archive universe, trailing liquidity and contiguous history segments | Learned return hurdle as the primary trade trigger | Certified full listing/delisting and migration register |
| Spot cash ledger, delayed plans, position identity, fees/slippage, gap stops | Strategy selection logic in a separate module | Futures funding, mark-price liquidation and order-book execution replay |
| Daily loss and permanently latched drawdown stop | No modification of recovered v0.3.1 behavior | Forward paper track record and live execution validation |

The new rule tests whether a stabilizing price base, improving relative strength,
completed weekly/monthly context and a favorable BTC regime identify an early
recovery. It does **not** claim that a price pattern establishes fundamental
undervaluation. The complete frozen rule and acceptance gates are in
[`CASHFLOW_ALPHA_PROTOCOL.json`](CASHFLOW_ALPHA_PROTOCOL.json).

## Chronology and costs

Only completed bars are used. The weekly Sunday signal is observed Monday and
executes no earlier than Tuesday's open (D+2). Current rankings and future returns
do not choose the universe. A 240-day contiguous window is required; gaps and the
known LUNA identity break reset eligibility. Current survivors are not substituted
for historical archive membership.

The first baseline uses unborrowed spot assets. Funding is therefore exactly zero,
not a missing futures expense silently omitted. Base costs assume 10 bps fee plus
10 bps slippage **per side**; stress costs assume 20 plus 30 bps per side. These are
scenario assumptions, not a claim about an account's actual fee tier. Stress cases
rerun sizing and fills. They are not a uniform subtraction from the base return.

At an unobservable held market's open, the last observed mark remains an accounting
mark, never an invented executable quote. Other observable markets may execute.
The missing position is written off at day end with zero recovery. This is a
conservative missing-market scenario, not an assertion about actual delisting
recoveries. Known identity boundaries cannot be crossed by held positions.

Stop gaps execute at the worse opening price. If a daily bar touches both the stop
and profit target, the inherited broker assumes the stop first. Daily bars do not
resolve intraday order or depth. The 3% daily loss and 15% maximum drawdown rules are
halt triggers; gaps, outages and intraday uncertainty can exceed these numbers.

## Walk-forward interpretation

2018 provides warmup. The main test is one uninterrupted account from 2019-01-01
through 2026-09-30 UTC. Separate yearly accounts begin with fresh capital and risk
state, observing only prior bars at each decision. Those yearly diagnostics are
**never stitched** into an investable account that bypasses a permanent halt.

The policy is fixed and has no fitted parameters. This is chronological
fixed-policy walk-forward evaluation, not rolling model fitting or an untouched
historical holdout. Acceptance gates were frozen before this strategy's results.
Any future changes after inspecting these results constitute a new experiment.

## Reproduce

Python 3.10+, standard library only. Run from repository root. `--end` is exclusive.
Use the saved source archive for exact replication; upstream ZIPs can be revised.

```sh
python -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests scripts
python one.py --help
python one.py status --workspace workspace_cashflow_check

python -m one_trading.historical_data \
  --db output/market/market.sqlite3 --cache output/market/cache \
  --start 2018-01-01 --end 2026-10-01 --workers 16 --memory-build

python scripts/run_cashflow_alpha.py \
  --archive output/market/market.sqlite3 --output output/cashflow-v1

python scripts/validate_cashflow_alpha.py \
  output/cashflow-v1/base_continuous.json output/cashflow-v1/stress_continuous.json \
  --archive output/market/market.sqlite3 --prefix-date 2023-01-01 \
  --output output/cashflow-v1/independent_continuous_audit.json
python scripts/validate_cashflow_alpha.py \
  output/cashflow-v1/base_20*.json output/cashflow-v1/stress_20*.json \
  --output output/cashflow-v1/independent_fold_audit.json
python - <<'PY'
import json
from pathlib import Path
p = Path('output/cashflow-v1')
continuous = json.loads((p/'independent_continuous_audit.json').read_text())
folds = json.loads((p/'independent_fold_audit.json').read_text())
(p/'independent_validation.json').write_text(json.dumps({**continuous, **folds}, indent=2)+'\n')
PY
python scripts/analyze_cashflow_target.py \
  --run output/cashflow-v1/base_continuous.json \
  --output output/cashflow-v1/base_cashflow_target.json
python scripts/analyze_cashflow_target.py \
  --run output/cashflow-v1/stress_continuous.json \
  --output output/cashflow-v1/stress_cashflow_target.json
```

Each output directory must be new. Protocol, code hashes and archive hash are saved
before performance calculation. Original ZIP bytes and source metadata are inside
the SQLite archive. Raw data and individual simulation records are intentionally
excluded from Git; reviewed aggregate evidence is published separately.

The completed [v1 result report](experiments/2026-10-25/RESULTS.md) and
[independent review](CASHFLOW_ALPHA_INDEPENDENT_REVIEW.md) record **REJECTED**.
This conclusion applies to v1. It is not an assertion that every future strategy
or a capital-growth route to the long-term objective is impossible.

## Cashflow interpretation

KRW 5m/10m monthly from KRW 20m starting capital requires 25%/50% monthly net return
before capital growth, taxes and currency changes. At 0.5% trade risk, this equals
50R/100R on initial equity. Neither annual return divided by twelve nor unrealized
mark-to-market profits establish dependable monthly cash withdrawals.

Historical withdrawal replays and resampled intervals describe the observed
experiment. They do not estimate a calibrated future success probability. Returns
are denominated in USDT; KRW illustrations apply a constant conversion factor and
exclude taxes, cash-transfer costs and FX movements.
