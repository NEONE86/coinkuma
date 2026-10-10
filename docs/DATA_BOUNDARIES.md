# Reproducibility and data boundaries

## What this public checkout can reproduce

The 99 offline unit/regression tests generate synthetic prices and temporary SQLite
records in-process. Network tests are mocked. No saved market database is required.
The configuration contains research defaults rather than real account positions.

## What it cannot reproduce alone

The original historical base/stress replay requires a price archive and historical
comparison outputs that are deliberately excluded from publication. Therefore the
public CI runs tests only; it does not validate historical replay parity, historical
profitability, live market connectivity, or forward performance.

`python one.py replay` defaults to `archive/observer_v02.sqlite3`, which is absent.
A missing archive is expected to fail. Use `--archive` only with a compatible file
you are authorized to use. The reader expects the `snapshots` schema implemented
in `one_trading.observer.Store`, including raw response bytes, their SHA-256,
request paths/parameters and timestamps. `load_history` in `one_trading.research`
reads exchange-time and daily-kline snapshots; BTC history must contain at least
360 bars for portfolio replay. Hash consistency is not proof of source authenticity.

Do not relabel generated synthetic tests as market performance. A newly collected
archive is a different dataset and cannot establish parity with omitted records.
Keep new runtime data and experiment outputs private unless separately reviewed.

## Chronology and execution assumptions

Features use completed bars and labels must mature before learning. The replay
observes a signal after day D closes and schedules fills from D+2. Forward paper
operation instead uses fresh observed quotes when pending plans become eligible;
these are different execution-price assumptions.

Training uses overlapping multi-asset labels, so raw sample counts are not counts
of independent observations. A dataset already used for development or repeated
inspection is not untouched out-of-sample evidence. Survivorship and liquidity
selection, estimated costs and omitted real-market execution effects remain risks.

There is no real-order adapter. Test success and software reproducibility cannot
establish that a trading strategy will earn money.
