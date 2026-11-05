# Fixed offline opportunity study

This is reused historical spot-long research, not an AI strategy, prospective
validation, or a claim of executable exchange fills. No network/provider adapter
is called. PR25 remains unchanged; the live PAPER runner is not modified.

`configs/opportunity_v1.json` was committed in `aab8552` before outcomes were
computed. No grid search or threshold fitting is performed. All 4 horizons and
4 reward/risk settings are retained, including losers. Half-time partitions are
exploratory discovery/validation on already-used data, not a pristine holdout.
Cross-boundary holding windows are purged. There is no selection based on future
MFE, MAE, prices or hit order. Cash is an identical-period zero-return baseline.

## Exact economics

- Signals use only PR25's 30/60/300-second received-frame features. Bounded
  windows preserve the anchor at/before cutoff minus 300s and match full-prefix
  numerical features. Their hash covers the window, explicitly not the full tape.
- Every 300 seconds, after 300 seconds warmup, one portfolio slot chooses the
  strongest absolute 300s return among eligible assets, with symbol tie-break.
- Trend: 300s return exceeds estimated round-trip cost and 60s return is positive.
  Reversion: 300s location <=20%, positive 30s return, and 300s realized volatility
  >= half round-trip cost. Imbalance assist additionally requires L1 imbalance
  >=0.2 on the trend signal. None is an established economic causal mechanism.
- Volatility is sqrt(sum squared log midpoint returns), unannualized. Net stop
  loss fraction is max(0.001, prior300s volatility) plus estimated round-trip cost
  (two 10bp fees, two 5bp slippages and current spread). Net targets are 2/3/5/8
  times that loss fraction. No leverage or shorting. Funding is not applicable.
- Each purchase is 0.11% of current 10,000 USDT starting equity, about11 USDT.
  This is a deliberately small fixed research notional, NOT evidence of capacity
  at larger sizes. Original engine position/exposure caps and 10 USDT minimum
  are retained. Both sides require <=1% of visible L1 quantity. No queue model.
- Decision latency0.5s plus execution delay2s; use the first later sampled frame.
  The original receipt must be after decision completion. Max age30s/spread20bp.
  Fees/slippage are assumed fixed protocol costs, not verified account fee tiers.
- Targets/stops use observed liquidation bid after all costs. They are triggers,
  not guaranteed execution prices. Delayed fill can exceed a stop or miss a target.
  A rejected exit waits for the horizon attempt; another rejection leaves an open
  position, marks it at the partition's last bid less hypothetical costs, and
  halts further entries. A mark is never a fill. No unlimited exit retry.
- Every position blocks another until exit. Portfolio equity compounds actual
  cost-adjusted fills; log growth is log(final/initial), per-trade log growth is
  log(1+PnL/prior_equity). Drawdown includes intermediate bid liquidation marks.
  Open/unobserved cases prevent candidate promotion. No annualization.

## Interpretation limits

Full-horizon MFE/MAE use future data only as labels. Positive maximum excursions
are hindsight upper bounds and cannot establish an entry/exit strategy. All-grid
labels overlap; do not sum their profits, estimate independent win-rate confidence,
or treat asset/RR variants as independent trials. Clean-closed summary statistics
are explicitly conditional on observed gaps and fill feasibility; rejected/open/
censored denominators are reported separately. Full-horizon gaps conservatively
exclude a label even if a strategy exited earlier. One-second frames cannot prove
the intra-second path, actual exchange fills, or capacity between observations.

The binary ideal break-even win rate1/(1+RR) ignores timeout returns, delayed
overshoots and open positions. It is not the break-even rate of this mixed-outcome
strategy. Report observed positive-return rate and mean realized return separately.
At most6 non-overlapping4h blocks exist in24h, before correlation across assets.
Candidate promotion requires20 clean validation trades and positive log growth;
5+ clean validation trades with nonpositive growth rejects the fixed hypothesis.
Otherwise INSUFFICIENT, including a positive result from only a few trades.
No inference that increasing exposure would preserve return or fill capacity.

## Reproduce (private inputs and a NEW output path)

```powershell
python -B -X utf8 -m one_trading.ai_native.opportunity_study --source PRIVATE_CAPTURE --output NEW_PRIVATE_STUDY
python -B -X utf8 -m one_trading.ai_native.opportunity_audit --source PRIVATE_CAPTURE --study NEW_PRIVATE_STUDY
python -B -X utf8 -m unittest discover -s tests -v
python -B -X utf8 scripts/run_observer_audit.py --out NEW_OFFLINE_AUDIT
```

Before running, independently verify the capture against its original SHA256
manifest and the existing Bybit source auditor. `opportunity_audit` independently
rebuilds trade arithmetic, causal volatility risk, touch order, delays, source
receipt availability, depth, partition boundaries and portfolio drawdown. It does
not certify exhaustive strategy signal enumeration or genuine investment alpha.
Unit tests include bounded/full-prefix parity, future-prefix invariance, first
touch, tail censoring, depth, stale/pre-decision quotes, minimum notional, cash,
partition isolation, compound arithmetic, and six semantic tamper controls.

An initial implementation omitted SELL minimum notional. It was fixed without
changing frozen strategy parameters; the initial run is preserved separately and
the final code rerun. Source and all outcomes remain private. Public CI uses only
synthetic fixtures. Raw evidence must never be uploaded as public Actions artifacts.
