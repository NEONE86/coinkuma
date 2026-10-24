# Offline validation

Run from the repository root:

```sh
python -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests
python one.py --help
python one.py status --workspace workspace_public
```

The recovered suite contains 99 tests:

- Governance: 27; evidence gates, append-only records and risk calculations.
- Learning/execution: 32; matured labels, chronological invariance, delay,
  idempotency, fees, PnL, gaps, stops and position limits.
- Observer: 15; completed bars, malformed data, raw hashes and public endpoints.
- Pending-position context: 8; stale-plan and position-identity regressions.
- Research: 5; calendar boundaries, delayed entry and benchmark behavior.
- Runtime: 12; freshness, ledger configuration, locking, mocked cycles and failures.

Synthetic fixtures and temporary stores are created by the tests. No historical
price database or trade log is bundled. Success requires 99 tests run with no
failures/errors. Keep later feature tests separate from these recovered tests.

The public workflow tests Python 3.12 on Linux and checks CLI help/status. Its
result must be inspected on the exact published commit before claiming CI passes.
It does not run the original database-dependent replay comparisons.

Not validated here: Windows, Docker, real network operation, continuous forward
paper performance, exchange execution, or profitability on unseen data.

## Added historical workflow tests

The new `tests/test_historical.py` adds 21 independent regressions (120 total):
time-unit normalization, shortened terminal bars, malformed-row quarantine, source
checksums, derived-table tamper detection, incomplete/missing archives, cache
integrity, historical liquidity/availability, gap resets, retained loss labels,
writeoff accounting, prior-day regimes, benchmark cost/date matching, and complete
run invariance to future data. These remain synthetic software tests. Real-data
experiment evidence is documented separately in
[the result report](experiments/2026-10-17/RESULTS.md).

## Cashflow Alpha additions

The current complete suite has 143 tests: 120 inherited tests, 12 fixed-swing
regressions, 5 independent audit regressions and 6 cashflow arithmetic regressions.
The additions exercise non-vacuous future-prefix invariance, delayed weekly plans,
multi-fill campaign identity, original-entry risk anchoring, missing-market losses,
independent cash/inventory reconciliation and the distinction between realized PnL,
marked equity and withdrawal arithmetic. These remain software tests.

Separately, the [real-data independent review](CASHFLOW_ALPHA_INDEPENDENT_REVIEW.md)
checks all 18 accounts, reconstructs historical universes and physically truncates
future actual prices before rerunning. Despite passing these checks, the
[strategy result](experiments/2026-10-25/RESULTS.md) is **REJECTED**.
