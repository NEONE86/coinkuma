# Hourly dual-engine forward experiment

This is a separate public-data, paper-only engine derived from PR #9
(`cc7ccc8e50de8518b6b178801ad38eada9b687fb`). The existing daily ridge
runner and all prior research ledgers are unchanged. Runtime directories must
be outside every Git checkout. No exchange order, withdrawal, trading-key or
account endpoint exists in this package.

## Run and recover

From this PR's separate branch, using Python 3.12 or newer:

```powershell
python -m one_trading.autonomy --help
python -m one_trading.autonomy --runtime C:\ONE-private\forward init
python -m one_trading.autonomy --runtime C:\ONE-private\forward tick
python -m one_trading.autonomy --runtime C:\ONE-private\forward status
python -m one_trading.autonomy --runtime C:\ONE-private\forward export
```

The CLI verifies PR #9 ancestry and refuses main, master, the PR #9 branch,
and detached HEAD. Repeating tick resumes the current hour; it does not create
a second set of forecasts or paper positions. New hours get independent actual
HTTP captures. An interrupted collection retains receipts and raw hashes.
SQLite transactions make the nine policy decisions and nine shared questions
atomic. The 27 probability forecasts reference those shared questions.

For a controlled process restart:
```powershell
python -m one_trading.autonomy --runtime C:\ONE-private\forward tick --stop-after packet
python -m one_trading.autonomy --runtime C:\ONE-private\forward tick
```
The regression suite also terminates a real child process inside a transaction
and checks rollback, durable records and lock release. Controlled live restarts
and synthetic crash tests are reported separately.

Before continuous deployment, freeze the implementation:
```powershell
python -m one_trading.autonomy --runtime C:\ONE-private\forward seal
```
A sealed runtime refuses changed Python source. Use a new runtime for a new
implementation or configuration, retaining the old ledger. Each new context
also carries an implementation hash. Early development cohorts that predate
this field must be identified as bootstrap observations, not silently combined
with a changed strategy.

## Windows scheduler

```powershell
.\scripts\install_dual_engine_task.ps1 -Python C:\Python313\python.exe -Runtime C:\ONE-private\forward
.\scripts\install_dual_engine_task.ps1 -Python C:\Python313\python.exe -Runtime C:\ONE-private\forward -Activate
Get-ScheduledTaskInfo -TaskName ONE-DualEngine-Forward-v1-20261029
Start-ScheduledTask -TaskName ONE-DualEngine-Forward-v1-20261029
Disable-ScheduledTask -TaskName ONE-DualEngine-Forward-v1-20261029
Enable-ScheduledTask -TaskName ONE-DualEngine-Forward-v1-20261029
```

The first install command validates without activating. Activation creates a
limited-permission, current-user task, hidden window, five-minute repetition,
IgnoreNew concurrency, bounded restarts and StartWhenAvailable. Repetition has
no seven-day expiration. The host must be awake and that user logged in;
this is not a remote server and does not wake a powered-off computer.
The installer pins the discovered Codex and Git executable paths privately because
Task Scheduler does not necessarily inherit the interactive PATH.
The wrapper launches a hidden Python process with separate stdout/stderr files, so PowerShell native-error handling cannot truncate Python tracebacks. Scheduler logs and task XML are private. A fresh scheduler query, rather than
the mere existence of past runs, determines FORWARD_RUNNING.

## Frozen CASHFLOW experiment

Universe: BTCUSDT, ETHUSDT, SOLUSDT, extensible to at most 50 configured symbols.
Completed one-hour bars provide relative returns against the other configured
assets. This is a broad crypto proxy, not a verified sector or beta hedge.
Book and derivatives snapshots are contemporaneous overlays with their own
receipt times. News is explicitly missing.

- CF_BASE: relative one-hour return below -0.5%.
- CF_NUMERIC: that price condition plus base-asset OI decrease greater than 1%,
  last settled funding normalized by its observed interval at most 0.01%/hour,
  taker-buy quote-volume share at least 35%, and absolute basis at most 0.5%.
- CF_AI_MEMORY: an actual closed-input model classification, probabilities and
  decision using the same packet, plus only previously resolved 72-hour
  nonoverlapping episode memory. Missing numeric inputs prevent entry. A LONG
  also requires the forced-reduction hypothesis class and 24h probability at
  least .55; this is still an unproven classifier gate, not an expected-profit proof.

The fixed .55/.58 numeric-policy probabilities are uncalibrated research priors.
The AI model is not substituted with a heuristic when unavailable.
MODEL_NOT_CONNECTED, MODEL_TIMEOUT and MODEL_INVALID_OUTPUT leave the first
two policies running and the AI policy at NO_TRADE with no scored probability.

The default backend uses existing Codex login and the configured model through
`codex exec --ignore-user-config --ephemeral --sandbox read-only`, JSON schema,
disabled shell/web tools and bounded timeout. Inputs, prompts, responses,
stdout/stderr, hashes and actual call timestamps stay private. Each attempt has
its own directory. No API key is read, copied or published.
[Official noninteractive documentation](https://developers.openai.com/codex/noninteractive/).

The shared outcome deadlines are the next minute after registration plus
6/24/72 hours. Endpoints must be the exact completed one-minute bar ending at
that deadline; missing endpoints remain unresolved. Origin prices are observed
snapshot mids, so outcomes are explicitly **signal markouts, not fills**.
The primary CF label is cost-net asset markout minus the gross equal-weight
other-asset benchmark. That conservative hurdle is not an executable hedged
portfolio. All three policies use the same hurdle. Reports stratify horizons,
show common-question equal-risk diagnostics, and do not count overlapping
horizons as independent observations.

OI quantity is base asset, not USDT notional; a notional decline is not evidence
of liquidation. Funding uses actual settlement timestamps, with announced
interval metadata retained separately. REST depth has no exchange event
timestamp; its field is null, not invented. Exchange clock offsets up to three
seconds are explicitly bounded; local availability always means receipt time.
[Binance USD-M market data documentation](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics)
limits OI statistics to the latest month. Actual out-of-window responses belong
in the private availability audit.

## Paper execution and capital

The 20,000,000 KRW research cap is split into 12,000,000 CASHFLOW and
8,000,000 CAPITAL_GROWTH. Policy comparison sleeves are counterfactuals, not
additional capital. Only the frozen CF_NUMERIC and G_INDIRECT champions count
toward combined actual paper exposure. KRW/USDT = 1400 is a fixed research
conversion assumption, not an FX quote.

CF proposals wait at least 60 seconds, expire after two hours, and use a newly
received book. Positions hold 24 hours with a 5% stop; missing exit liquidity
keeps the position open and records the failure. No synthetic close is invented.
Sizing is 5% of the CF sleeve, maximum symbol cost 2m KRW and CF total cost 6m.
Duplicate open symbols, cash limits, portfolio losses, missing marks, crossed
books, spreads over 20bps and insufficient depth reject entry.

Fills consume at most 1% of each observed depth level within 50bps of the best
quote, add 5bps slippage and 10bps fees per side. This is conditional visible-book
simulation; exchange lot rules, queue position, hidden liquidity, actual latency
and live execution are unvalidated. Funding is NOT_APPLICABLE_SPOT, explicitly
zero; derivative execution is rejected, so missing perpetual funding cannot
be turned into free profit. No hedge is placed. There is no withdrawable real
cashflow. Signal costs are 25bps/side with a 100bps/side replacement stress.
Paper stress is an additional 100bps/side shock on simulated fills.

## Growth propagation

```powershell
python -m one_trading.autonomy --runtime C:\ONE-private\forward register-growth C:\ONE-private\growth-spec.json
```

The private specification supplies three distinct root URLs/mechanisms,
direct and two indirect candidates, claims, assumptions, competitors, harmed
candidates, economic tests and invalidation. The system captures original
HTML and receipt times, checks source anchors, collects prices, calls the model,
then registers G_DIRECT, G_SECTOR, G_INDIRECT and G_LAGGED at one cutoff.
First observed means first captured by this system, not first reported worldwide.

Economic predicates have 90-day deadlines; price comparisons have 180-day
deadlines, never CF's 72 hours. Future source pages are captured daily. A page
change alone cannot establish an economic predicate. **Structured economic
adjudication and the lagged policy's confirmed-entry adapter remain incomplete**;
that policy stays cash and is explicitly WAIT_ECONOMIC_CONFIRMATION.
At the price deadline, exact endpoint collection computes the counterfactual
control returns; the direct comparison leg receives the same costs.
Missing origin/endpoint data remains unresolved. These are not growth fills.

All growth candidates stay WATCH/REJECT until current valuation, dilution,
runway and holder capture are sufficiently evidenced. The v3 upper mandate
named by the supplied instructions was not provided and is recorded missing.
No historical CAKE/UNI record is revised.

## Validation and limitations

```powershell
python -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests scripts tools
python one.py --help
python one.py status --workspace workspace_public
python -m one_trading.economic_research --help
python -m one_trading.autonomy --help
git diff --check
```

`tests/test_autonomy.py` covers the twelve requested failure domains, including
wrong ref, clocks, units, common policy context, future leakage, immature and
missing outcomes, model failure/schema, duplicate/restart, risk and accounting,
and three growth causes/four controls. Fixtures are synthetic and prove software
properties only. Private review bundles contain real ledgers, raw responses,
model artifacts, scheduler evidence and a standalone stdlib recalculation script.

No champion is promoted automatically. Software readiness, scheduler activity,
forecast quality, economic edge and exchange feasibility are separate findings.
Until sufficient resolved prospective, nonoverlapping and cost-stressed evidence
exists, EDGE_PROVEN is false. LIVE_TRADING_ENABLED is always false.
