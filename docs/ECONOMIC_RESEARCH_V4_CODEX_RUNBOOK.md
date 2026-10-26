# v4 one-shot runbook

Run from the repository root with Python 3.10+; standard library only. The runtime
directory must be outside **every** Git checkout. No key or exchange account is
needed. CLI failures return 1, partial source coverage returns 2, completion 0.
Commands and JSON below describe existing interfaces; no future scheduler is
installed. Existing historic research requires its own exact commit and original
archive; this runbook does not recreate old performance.

## Public, reproducible software checks

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_economic_research_v4.py -v
python -m compileall -q one.py one_trading tests scripts tools
python one.py --help
python one.py status --workspace workspace_public
python -m one_trading.economic_research --help
```

The v4 suite has 22 original tests, including T01–T14, and 25 independent-review
regressions in `test_economic_research_v4_fixes.py` (228 repository tests total).
The synthetic mature-policy test
`test_full_synthetic_mature_policy_pair_attribution_and_rejection_history` freezes
the challenger before new inputs, records two separate decisions, resolves a
future terminal price under a test-only controlled clock, reports a missed winner,
records an attribution hypothesis and retains a rejected challenger. Test clocks
are mocked only in tests; the CLI does not accept a forged registration clock.

## Fresh source capture

```sh
python -m one_trading.economic_research init --workspace ../v4-runtime
python -m one_trading.economic_research collect-once --workspace ../v4-runtime --sources docs/ECONOMIC_RESEARCH_V4_SOURCES.json --output ../v4-runtime/collection.json
```

Online collection always refreshes. A second fetch may produce a new snapshot,
including a harmless page-render change. It never overwrites the old bytes.
Every actual request attempt gets a durable receipt; pre-request rejection or
post-capture parsing failures also remain in the attempt log. A failed request is
not a new observation and does not silently reuse an old source as current.
403/429/geographic restrictions are reported, not bypassed by rotating hosts.
Offline collection uses explicit `--offline` and reports OFFLINE_REUSE.

The source JSON is an array of `{ "url": "https://...", "source_kind": "..." }`.
Optional `price_asset_id` normalizes a Binance 24hr ticker only after that explicit
identity exists. The source contract is a deliberately small reviewed list, not
an arbitrary external URL loader. Source text is untrusted data, including any
instructions it happens to contain.

## Local current run: exact command sequence

The delivered private sibling directory `../v4-runtime` contains source files,
SQLite, receipts and the Codex-authored AAVE/UNI/CAKE analysis JSON. Those files
are intentionally not in the public repository. A fresh public checkout can run
the synthetic tests and capture fresh sources, but cannot reproduce the current
cards without the private snapshot and analysis files.

For each asset the local run executed `ingest`, indexed its already captured
ticker with `collect-once --offline`, created `packet`, then called `register`.
AAVE was completed through report before expanding to UNI and CAKE.

```sh
python -m one_trading.economic_research ingest --workspace ../v4-runtime --file ../v4-runtime/AAVE_ingest.json --output ../v4-runtime/AAVE_ingested.json
python -m one_trading.economic_research collect-once --workspace ../v4-runtime --sources ../v4-runtime/AAVE_price_sources.json --offline --output ../v4-runtime/AAVE_price_indexed.json
python -m one_trading.economic_research packet --workspace ../v4-runtime --asset-id eip155:1:0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9 --output ../v4-runtime/AAVE_packet.json
python -m one_trading.economic_research register --workspace ../v4-runtime --file ../v4-runtime/AAVE_analysis.json --output ../v4-runtime/AAVE_registered.json
python -m one_trading.economic_research register --workspace ../v4-runtime --file ../v4-runtime/UNI_analysis.json --output ../v4-runtime/UNI_registered.json
python -m one_trading.economic_research register --workspace ../v4-runtime --file ../v4-runtime/CAKE_analysis.json --output ../v4-runtime/CAKE_registered.json
python -m one_trading.economic_research compare --workspace ../v4-runtime --file ../v4-runtime/candidate_comparison.json --output ../v4-runtime/comparison_registered.json
python -m one_trading.economic_research resolve --workspace ../v4-runtime
python -m one_trading.economic_research report --workspace ../v4-runtime --output ../v4-runtime/current-report.json
python -m one_trading.economic_research report --workspace ../v4-runtime --format markdown --output ../v4-runtime/current-report.md
python -m one_trading.economic_research audit --workspace ../v4-runtime
```

Re-registering the exact analysis is idempotent and preserves the actual original
registration time. It also rechecks the packet/raw evidence. To reproduce the
same bytes, pass the original report's `as_of` to `report --as-of <UTC>` and
`packet --as-of <UTC>`. A fresh `packet` without `--as-of` deliberately makes a
new information cutoff. A fresh source response cannot recreate an old receipt.

## Import contracts

`ingest --file` accepts a JSON array of `{"kind": ..., "data": ...}` objects in
dependency order. Kinds: `asset_identity`, `observation`, `economic_flow`,
`economic_event_revision`, `policy`, `policy_change`, `policy_change_review`,
`attribution`, `valuation_and_decision`, `policy_cohort`. Domain validators enforce referenced
records; the batch is transactional and a rejection survives outside the rollback.
The supplied protocol lists baseline fields; additional implementation fields
are demonstrated in the synthetic test fixture and private authored JSON.

`packet --asset-id` can be repeated for multiple explicitly registered subjects.
Packet inputs use the latest **available-at-cutoff** event revisions and prices,
plus their versioned evidence/identity/flow closure. `register --file` takes a
`thesis`, nonempty `forecasts` array containing all three target types, and one
`decision`. Numeric probabilities/amounts are decimal strings. Model-token
likelihoods are rejected as event probabilities. A zero reference-class base rate
must not replace an unknown one; use null.

`resolution_rule` fixes measurement identifier, unit, threshold/operator, exact
window, allowed source URLs (optional reviewed trailing-slash path prefixes),
missing-data and effective-time rules. `ignore_query_for_source_match` supports
fixed public endpoints with time/symbol query parameters. Every destination is
still checked against the source allowlist. Supported operators are GE/GT/LE/LT/
EQ for binary questions and NUMERIC for discrete numeric forecast distributions.
The analyst's period attribution must be reviewed; hashes cannot prove it true.

Observations require `value_type=DECIMAL|TEXT|BOOLEAN`. Numeric outcomes require
DECIMAL and recursive validation of every final input. TEXT cannot resolve a
numeric question. Source claims remain claims, including through multiple sums.
No ASSUMPTION ancestor can resolve an outcome. Arithmetic inputs must have the
same asset, unit and effective period; arbitrary period aggregation is unsupported.

`question_id` owns the immutable subject, target, rule, unit, source, benchmark and
deadline contract. Revisions must reference the preceding forecast, advance both
cutoff and registration, and use a newly registered thesis/packet. The original
economic window remains fixed even when part of it has elapsed. Both remaining
horizon (deadline minus cutoff) and actual lead (deadline minus registration) are
reported. Different horizons/leads are not pooled into one score. INITIAL selects
the first registered probability, LATEST_AS_OF the latest registered by as_of;
all revisions share the question's single resolution history.

Set `resolution_rule.observation_mode` explicitly for new questions. WINDOW_FINAL
(legacy default), END_STATE and TERMINAL_PRICE require the endpoint; a current
positive observation does not prove the final state. EVER_IN_WINDOW and
MONOTONIC_CUMULATIVE permit decisive partial positive GE/GT evidence. These modes
are part of the frozen contract. A known result anywhere in recorded observations
or a question's resolution history blocks prospective revision, even if omitted
from the new packet. Unrecorded external knowledge is outside this check. For a
legacy question whose temporal meaning was ambiguous, do not reinterpret its
contract retrospectively: record a separate, explicitly defined question.

For a price-only decision update using the existing thesis and forecasts, use
`ingest` with kind `valuation_and_decision` and `supersedes_decision_id`. Do not
submit a new thesis bundle that points at old thesis revisions.

`resolve --file <resolution.json>` accepts `resolution_id`, `forecast_id`,
`status`, `explanation`, `evidence_ids`, `observation_ids` and
`supersedes_resolution_id`. The actual outcome is recomputed from the admissible
numeric observation and the frozen rule. An imported outcome value is not trusted.
With no file, `resolve` only marks due questions without evidence DATA_MISSING;
it does not fetch or invent outcomes. Corrections require a new ID and predecessor.

`compare --file` accepts either the candidate-comparison format used above or
`{"change_id":"...","decision_ids":["old-policy-decision","new-policy-decision"]}`.
The latter requires a frozen change and both policies' separate records on the
same post-preregistration information vintage. Initial results remain pending;
rerun after admissible terminal-price resolution for a new comparison snapshot.
The first comparison enrolls a pair **before** its outcome or deadline. Endpoint
contracts (including instrument/price specification, sources and benchmark) and
one common question/outcome must match. Before any cohort outcome, freeze the
complete enrolled event set using `ingest` with kind `policy_cohort` and data
`{"cohort_id":"...","change_id":"...","pair_ids":["pair-..."]}`. No more pairs
can be added to that cohort. Completion requires every frozen pair to resolve;
PENDING/missing pairs remain in report coverage. Pair result records contain no
mutable global forecast coverage, so unrelated imports do not break replay.
Reports separately calculate full cohort coverage at the requested as_of. This
remains a one-unit proposal markout diagnostic, never account performance or
automatic policy promotion. `CAPITAL_REVIEW` still grants no authority.

`audit` now adds a read-only F01–F13 semantic impact inventory to raw/storage/
packet checks; any detected issue yields a failing CLI status. Legacy resolved
records are revalidated before scoring or policy comparison. A bad label fails
closed until an explicit valid superseding resolution is registered. Auditing
does not rewrite old data or certify source truth.

The [blank change template](ECONOMIC_RESEARCH_V4_POLICY_CHANGE_TEMPLATE.json) is
intentionally incomplete and must **not** be registered as real research. It is
not the design document's hypothetical token example and it is not a learned
policy. A real change requires evidence, one major modification, costs, a frozen
endpoint and an unused future comparison stream.

## Practical boundaries

The current cards used an observed public **last-trade** ticker, not an executable
quote or fill. Their modeled per-side costs and 30% hurdle are explicitly
unvalidated research assumptions. Actual terminal distributions, counterfactual
fee attribution, complete supply reconciliation and a risk-configured paper
portfolio require later work. No order or account API is implemented here.

Keep raw bytes, receipts, private JSON, logs, journals, SQLite and model responses
outside Git. Publish only code, synthetic fixtures and separately reviewed
summaries. Adding this package does not update earlier whole-directory freeze
hashes; historical studies still reproduce at their original exact SHAs.
