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

## Second independent review: R2-01 through R2-04 (v4.2)

The reviewed baseline is `e140232eddd2993799e7795fd2f18f4c7a450ac0`.
Its supplied 20 cases reproduced 15 controls and five defect demonstrations.
Those original scripts remain evidence, not acceptance tests. The corrected
acceptance run preserves the 15 control function bodies and replaces only the
five assertions that expected unsafe behavior. Nine focused regression methods
in `tests/test_economic_research_v4_r2.py` cover the four findings and boundaries.
The full suite now contains 237 tests (228 inherited plus nine R2 methods).
On a Windows console using CP949, run the documented Python commands with
`python -X utf8` so the existing CLI's Unicode help text can be printed.

| Finding | Cause and repair | Acceptance result |
|---|---|---|
| R2-01 | String spelling entered fact hashes. `semantics.py` defines `semantic-fact-v2`; evidence, events, audit and scoring recompute it. | Integer/string/decimal aliases cannot be summed or create new clusters; unique value 1 against threshold 2 yields outcome 0 and Brier 0.64. Legacy duplicate sums fail closed until a linked valid resolution is appended. |
| R2-02 | A thesis context list was used as a measurement target. `contracts.py` now fixes one exact `target_asset_id`; registration, resolution, decision linking, policy comparison and audit check it. | B=200 cannot resolve A; A=50 versus forecast 200 has MAE 150. Separate A/B questions work in the same context. Target changes in revisions are rejected. |
| R2-03 | One question had only one prediction lineage. Forecast chains and INITIAL/LATEST selection now use `(question_id, policy_id)`; the outcome remains question-wide. | Same packet/cutoff permits predictions 100 and 80. Common result 50 gives MAE 50/30 and, from entry 100 with zero costs, both proposal payoffs -50%. Revising one policy to 90 yields latest MAE 40/30 while initial scores and both roots remain intact. |
| R2-04 | Truthy strings were treated as permission. `strict_bool` validates optional contract flags at registration and all later contract revalidation. | Strings, numeric 0/1 and null are rejected. JSON false rejects SOURCE_CLAIM; true retains SOURCE_CLAIM provenance in resolutions and training-label exports. |

### Numeric identity and supported input contract

DECIMAL observations accept JSON integers or finite decimal strings. JSON source
fractional/exponent tokens are decoded with `parse_float=str`; there is no float
conversion. Boolean, float, null-as-a-number, collection and programmatic Decimal
inputs are rejected (null with an explicit missing reason remains missing data).
Signed zero, trailing zeroes and exponent spellings canonicalize using Decimal's
exact coefficient and exponent, without context-rounded `normalize()`. Timezone
aware period bounds canonicalize to the same UTC instant. Identity also includes
asset, metric, unit, value type, source byte hash and exact locator. Different
assets, periods, metrics, units and source facts are not merged merely because
their numbers match. Identity versions and new semantic hashes never rewrite
serialized source bytes, observation bodies, old fact hashes or ledger envelopes.
Alias observations keep the first parsing time; source availability still comes
from the original raw fact. Analyst-assigned periods are not verified chain data.

### Targets, policy chains and compatibility

`target_asset_id` is separate from `thesis.subject_asset_ids` (context). Absence
has exactly one compatibility rule: a thesis with one subject supplies that
target. Multi-subject questions require an explicit target. No prose inference
is performed, and composite measurement targets are explicitly unsupported.
Legacy ambiguous questions produce an audit issue and fail closed, requiring
review and a new explicit question rather than a guessed retroactive target.

For independent forecasts, supply `policy_id` referencing a frozen registered
policy whose registration precedes or equals the packet cutoff. The common
question contract excludes predictor identity. Same-policy roots cannot reset
history; parent, advancing cutoff/registration and prospective evidence guards
remain. A policy registered after the cutoff or a result cannot gain a valid
prospective forecast through a new name. Missing policy IDs retain a separate
`LEGACY_UNATTRIBUTED` compatibility chain; they are never silently reassigned to
a decision policy. The legacy shared-forecast action comparison remains valid.
Explicit forecast policies must match the linked decision policy.

Every question has one canonical resolution history shared across all policy
chains. A new resolution must link its predecessor and supersedes it for every
policy; there is no per-policy choice of labels. Score rows and selection include
policy IDs, units and remaining horizon. Counts separately report policy forecast
evaluations, unique questions and event clusters; clusters are not evidence of
statistical independence. Scoring rechecks semantic facts in selected packets,
so legacy duplicate cluster labels cannot inflate coverage. Pending outcomes
continue to contribute zero accuracy labels.

### Legacy contamination and linked corrections

`audit` reports per-record R2 issues without editing history. Raw hash validity
alone is not enough. Legacy observations, recursive derived inputs, resolutions,
targets and boolean fields pass through the current validators. Scoring and
training export fail closed on an inadmissible resolved record; no partial labels
are returned from a failed scoring call. For a valid unchanged question with an
invalid old outcome, register a new valid observation and use the existing
`resolve` command with `supersedes_resolution_id` pointing to the old resolution.
The corrected outcome is shared by all policies. Audit retains the old issue and
lists the correction link; it never erases historical contamination. Invalid
contracts require explicit review; no automatic contract repair is attempted.

All three boolean contract fields (`accept_source_claim`,
`ignore_query_for_source_match`, `outcome_already_observed`) use the same strict
validator. Only the first was independently reproduced as a defect; application
to the other two is preventive consistency, not another claimed finding.

The supplied real runtime is audited on copies and delivered outside public Git
with raw snapshots, receipts, original/current SQLite and JSONL, packet inputs,
preservation tables, independent standard-library arithmetic verification and
offline report regeneration. No new market inputs or forecast assumptions are
introduced for this repair. Existing price-only decision revisions remain
separate from the original twelve forecasts. These are implementation and stored
record checks, not independent economic truth or investment performance proof.
