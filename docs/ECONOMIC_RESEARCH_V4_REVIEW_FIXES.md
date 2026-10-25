# PR #9 independent-review corrections (v4.1)

Baseline: `429b480aa5bacf92a0d57be438c593a0f58c9290`, unchanged PR8 parent
`78e927b7712a9333a59ff99c36c4e7e8e359d239`. The supplied original proofs ran at
that clean HEAD: 16 outputs from three scripts, including controls and overlaps.
The baseline's 203 tests passed while the defects reproduced. Those scripts are
pre-fix evidence, not post-fix acceptance tests. Private source/price/ledger
files and proof output stay outside Git.

| Finding | Correction | Regression in `test_economic_research_v4_fixes.py` | Original real-ledger impact |
|---|---|---|---|
| F01 | Explicit numeric types, raw locator equality, numeric-only outcomes | `test_F01_*` (raw 0, p=.8 → Brier .64) | All 18 observations have types; no false outcome |
| F02 | Recursive raw provenance, periods, times, arithmetic, cycles and claim/assumption rules | `test_F02_*` | CAKE derived source claim remains a claim; no resolved labels |
| F03 | Immutable measurement window separated from advancing revision cutoff | `test_F03_*`, `test_F03_F07_*` | 12 original roots unchanged; no original amendment attempted |
| F04 | Asset/rule/source/benchmark/deadline contract, one root, later clocks, known-result guard | `test_F04_*` | 12 unique contracts, no changed-question collision |
| F05 | Distribution/rule unit equality for every numeric target | `test_F05_*` | Three numeric price distributions match their units |
| F06 | MAE groups preserve subject, measurement definition, unit and exact horizon/lead | `test_F06_*` | No resolved numeric scores existed |
| F07 | Same endpoint/question/outcome; future pair enrollment and complete frozen cohort | `test_F07_*`, `test_F03_F07_*` | No real policy-pair outcome existed |
| F08 | Stored joint leaf probabilities; actual horizon, distributions, execution state | `test_F08_*` | Three original JOINT trees; no probability display error |
| F09 | Bundle thesis/forecast/decision consistency, atomic rejection and replay checks | `test_F09_*` | All three original bundles have consistent links |
| F10 | Immutable pair result separated from report-time ledger/cohort coverage | `test_F07_common_outcomes_and_F10_*` | No real policy comparison replay existed |
| F11 | Effective overdue status becomes DATA_MISSING without editing old PENDING | `test_F11_*` | All 12 real questions still before deadline |
| F12 | In-request and cross-flow leg/reference uniqueness before storage | `test_F12_*` | No duplicate original legs/references |
| F13 | Raw fact/locator/asset/measurement/unit/period/value identity, alias timestamps | `test_F13_*` | Three distinct events; no alias-inflated clusters |

All original 152 ledger envelopes (body, hash and recorded_at), 12 forecasts,
three packets and three initial decisions were preserved. The private audit
lists every inspected ID and explicitly distinguishes unexercised branches from
damaged records. No original outcome required invalidation. Three fresh-price
decision revisions retain original thesis, probabilities, scenario prices, costs,
hurdle and fixed endpoints; no old forecast was re-registered or backdated.

The independent review bundle is delivered privately and includes both runtime
copies, ledger JSONL exports, raw source bytes and receipt hashes, authored
analysis and new price-only decisions, question contracts, reports, semantic
impact inventory, code snapshot, independent arithmetic verifier and commands.
It is intentionally absent from this public branch.

## Reproduction

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p 'test_economic_research_v4*.py' -v
python -m compileall -q one.py one_trading tests scripts tools
python one.py --help
python one.py status --workspace workspace_public
python -m one_trading.economic_research --help
```

228 tests pass locally: the original 203 plus 25 new regressions. T08's fixture
now uses a later packet/thesis when amending a probability; its preservation
assertions remain. The combined F03/F07 test executes new evidence → same-question
revision → shared outcome → INITIAL/LATEST scoring → common policy payoff → repeat
comparison → report regeneration. All of that path is **synthetic software
verification**, not real market outcomes, probability calibration or profitability.

## Limits of the checks

Source hashes establish bytes, not source truth. Analyst-supplied effective-period
labels, causal attribution and full chain coverage still need independent review.
The known-result guard checks registered observations/resolutions, not unrecorded
human/model knowledge. For legacy questions, absent observation_mode means
WINDOW_FINAL; changing an ambiguous temporal meaning requires a separate question,
not a retroactive rewrite. Distinct periods are not blindly deduplicated, and
declared clusters are not statistical independent samples. Numeric score pooling
is deliberately conservative. No new normalization or trading strategy was tuned.

The real forecasts remain pending and score denominators remain zero. Current
entry choices still depend on uncalibrated judgment scenarios and an unvalidated
required-return assumption. No broker, account API, live order, deployment,
automatic policy promotion, or prior-PR merge is introduced.
