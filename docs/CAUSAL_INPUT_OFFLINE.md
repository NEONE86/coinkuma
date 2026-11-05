# Causal L1 input study (offline only)

Base PR24 input contains current bid/ask/mid/spread/exchange time and only the previous observed frame midpoint, NOT the previous AI decision midpoint. Top-of-book sizes and the intervening path were collected but omitted. Three HOLD responses and no matured one-hour labels do not establish statistically meaningful skill.

`causal_features.enrich(context, frames)` produces a separate augmented context without mutating originals. `offline_input_study` first verifies the original source and model/PAPER run, exports the three augmented inputs and replays deterministic BUY/SELL intents through the unchanged engine/auditor. The live runner is intentionally not enabled for this new input schema: a future separately approved change must bind augmented context in the live evidence and independent PAPER audit. No provider or collector imports/calls in the offline CLI.

## Fixed formulas

Receipt cutoff filters all frames before computation. IDs and receipts must be unique and increasing; quotes must not have future exchange timestamps. Windows30/60/300 seconds anchor at the latest receipt at or before cutoff-window. Anchor tolerance and every inter-frame gap are at most2.5s. Missing/gapped/stale windows carry explicit status and no numeric indicators; first request has no return history. Actual from/to timestamps accompany all windows (nominal window can extend by up to2.5s).

- Return = last_mid / anchor_mid -1; trend sign without fitted threshold.
- Realized volatility = sqrt(sum(log(mid_i/mid_previous)^2)), unannualized sampled-path variation, NOT forecast uncertainty. Expansion/contraction compares variation in the two independently sampled30s halves of a complete60s window; crossing-boundary return is excluded. Sample counts/cadence can affect it.
- Range = high_mid/low_mid -1; location = (last-low)/(high-low), null for flat range.
- Spread = (ask-bid)/mid*10000; mean and end-minus-start per window.
- L1 imbalance = (bid_size-ask_size)/(bid_size+ask_size); no claim of deeper depth or order flow.
- Freshness = cutoff minus exchange timestamp (plus separate frame age).
- Indicative round-trip cost = twice(fee_bps+slippage_bps)+current spread. This is a midpoint approximation using unchanged protocol, not actual future cost.
- Descriptive long scenario: target=prior300s high, stop=prior300s low; reward=target/current_mid-1-cost; risk=1-stop/current_mid+cost. Net reward/risk >=2 is a screening geometry only. Break-even probability=risk/(risk+reward) only if reward>0. Expected return stays null: no calibrated probability, catalyst or economic cause is available. No fabricated target beyond observed range.

One-hour/four-hour/daily current features remain unavailable in the10-minute capture. Older separate captures must not be joined across gaps into current continuous windows. No trade prints, volume, liquidations, funding, open interest, deeper depth or catalyst evidence can be reconstructed from orderbook.1 alone.

## Reproduction

`python -B -X utf8 -m one_trading.ai_native.offline_input_study --run-dir <private-existing-run03> --output-dir <new-private-folder>`

`python -B -X utf8 -m unittest discover -s tests -p test_causal_features.py -v`

Actual replay uses unmodified stored quotes, original per-symbol receipt timestamps,0.5s synthetic decision latency, default2s execution delay, fixed11USDT initial BUY and full SELL on frame21. These are functional injected decisions, not AI or independent market validation. Synthetic adverse packets separately test stale/prior-receipt/spread/depth, size clamp, exposure cap and drawdown latch. Existing tests retain same-frame, tamper and cost controls. No parameter selection based on replay profit.

Before next paid pilot: wire augmented context into live auditor, freeze schema/cost/strategy and a price-only enhanced-input control, warm up at least5min, preserve1h outcome follow-up and collect independent events under a predeclared sample/power budget. First use existing archives for cheap falsification; reused history and parameter tuning are not forward AI alpha.
