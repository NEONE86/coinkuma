# ONE Trading v0.3.1 — code-only research baseline

Latest independent study: [Cashflow Alpha v3 — Repricing Opportunity Atlas](docs/experiments/repricing-v3-2026-10-25/RESULTS.md).
Both fixed hypotheses are **REJECTED** after 473,267 historical label checks and
30 account backtests. No validated edge or live-trading approval. See the
[reproduction workflow](docs/REPRICING_V3.md); previous rejection results are preserved.

Experimental historical replay and forward paper trading. **Profitability is not
validated. No exchange-account access, real orders, or withdrawals are implemented.**
Do not provide API keys or fund an account to run this code.

## Quick start: offline tests

Python 3.10+; standard library only. The public export was tested on Linux with
Python 3.12. No installation of third-party packages is required.

```sh
python -m unittest discover -s tests -v
python one.py --help
python one.py status --workspace workspace_public
```

The 99 recovered tests use synthetic data, temporary databases and mocked network
responses. Passing them demonstrates tested software behavior, not a profitable
strategy or successful live exchange connectivity.

## Data boundary

This is a reviewed **code-only export**, not the complete historical recovery.
It includes implementation, synthetic tests, virtual research configuration and
these reviewed execution documents. Market-price databases, historical trades,
model decisions, network/audit logs, old reports and old Git history are excluded.

The default replay archive is deliberately absent. Consequently, this public
checkout alone **cannot reproduce the original data-dependent base/stress replay**.
No historical performance result is claimed by this export. If you independently
supply a compatible, lawfully obtained market archive, the existing interface is:

```sh
python one.py replay --archive /path/to/your/market.sqlite3 --output output/replay
```

An arbitrary price CSV is not a compatible archive. See
[reproducibility boundaries](docs/DATA_BOUNDARIES.md) for the expected format.

## Optional forward paper operation

These commands contact Binance's public market-data service and create local
simulation records. They require network availability; no API keys are used.

```sh
python one.py paper --workspace workspace_public
python one.py status --workspace workspace_public
python one.py run --workspace workspace_public
```

`paper` runs one cycle. `run` repeats until stopped with Ctrl+C (or a stop-required
condition). A stopped process does not continue in the background. Start a new
workspace after changing configuration; do not mix incompatible simulation ledgers.
This export does not establish successful network operation or provision a server.

`config.json` contains virtual research defaults, including virtual cash of 10,000
and assumed fees/slippage. These are neither real account data nor a recommendation
for anyone's capital allocation. The included watchlist is a research example,
not a buy recommendation.

## Scope and limitations

- Price-only ridge model, completed-bar features and matured 28-day labels.
- Delayed simulated execution, transaction-cost assumptions and persistent ledgers.
- Current-survivor/liquidity selection can bias historical experiments.
- Predictions are uncalibrated; the learning target differs from realized returns
  under stops and rotation. Repeated tuning on the same data is not new evidence.
- Order-book depth, partial fills, exchange order filters, taxes and real execution
  are not modeled. Risk limits do not guarantee losses stay below their values.

See [lineage and export scope](docs/EXPORT_SCOPE.md) and
[test scope](docs/TEST_SPEC.md). Strategy and execution modules are unchanged by
this publication; packaging and public documentation are separate additions.

## New historical-data experiment

The separate [historical workflow](docs/HISTORICAL_WORKFLOW.md) collects official
bulk data with raw-byte/checksum provenance, rebuilds the candidate universe from
historical liquidity, and runs continuous plus yearly walk-forward comparisons.
Use `python -m one_trading.walkforward --help` for the new runner. This new dataset
does not reproduce the missing original experiments. Historical listing coverage
and the fundamental revaluation engine remain incomplete; see the workflow for
the precise scope and assumptions.

The first new-data [result report](docs/experiments/2026-10-17/RESULTS.md) records
756 Binance histories and the 2019–September 2026 evaluation. Base continuous
return was +11.87%, versus BTC +2,150.33%; average market exposure was only 0.77%.
This does not meet the intended capital-growth objective.

## Cashflow Alpha research

The separate [Cashflow Alpha workflow](docs/CASHFLOW_ALPHA.md) implements a fixed
early-recovery price-proxy swing strategy on the PR #2 historical infrastructure.
The [frozen protocol](docs/CASHFLOW_ALPHA_PROTOCOL.json) specifies chronology,
spot costs, 0.5% position risk, 3% daily loss and 15% permanent drawdown-halt
triggers. It does not authorize live orders or treat price signals as fundamental
valuation evidence. The [actual results](docs/experiments/2026-10-25/RESULTS.md)
reject v1: +1.63% cumulative under base costs, -0.46% under stress costs,
113 closed campaigns and approximately 2.65% average exposure over 2019–September
2026. [Independent validation](docs/CASHFLOW_ALPHA_INDEPENDENT_REVIEW.md) reconciles
all 18 accounts and verifies real-data future-prefix invariance. Passing these
software checks does not establish a profitable strategy.

## Cashflow Alpha v2

The two preregistered PR #3 challengers are both **REJECTED**. On the same verified
2019–September 2026 history, stress net return / PF were -7.39% / 0.148 for
relative-strength recovery and +4.01% / 1.146 for controlled trend pullbacks.
Neither reached the 1.30 stress PF hurdle or the cashflow objective. All 48
accounts reconcile; all four continuous accounts independently reproduce and
preserve their past after future data deletion. See the
[Korean result report](docs/experiments/cashflow-v2-2026-10-25/RESULTS.md),
[independent calculation review](docs/experiments/cashflow-v2-2026-10-25/INDEPENDENT_REVIEW.md)
and [reproduction workflow](docs/CASHFLOW_ALPHA_V2.md). Earlier rejection results
remain unchanged. This is research evidence, not live-trading approval.

## Bounded economic-effect collection

The [Ethereum hook collector](docs/BOUNDED_CHAIN_COLLECTOR.md) captures one frozen
block range with raw RPC receipts, transaction/log reconciliation and offline
replay. It keeps direct gross transfers, incomplete coverage and unidentified
causal increment separate, and does not resolve pending forecasts.
