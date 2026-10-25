# Economic Repricing Research v4

**v4.1 correction:** the independent review of PR #9 at `429b480` reproduced
F01–F13 despite the original 203 passing tests. The fixes and 25 new assertions
are described in [the correction record](ECONOMIC_RESEARCH_V4_REVIEW_FIXES.md).
Do not treat the original test result as approval of the pre-fix semantics.

**Implemented one-shot research, not validated investment edge.** The first real
manual-import run selected UNI for research (`PRIORITIZE / WATCH`), retained CAKE
for observation (`MONITOR / NO_TRADE`), and excluded the unconfirmed continuing
AAVE buyback thesis from current selection (`ARCHIVE / NO_TRADE`). No asset was
entered. Twelve prospective forecasts are pending. These are reviewed summaries;
raw responses, observed prices, detailed analysis and the ledger stay outside Git.

## Lineage and preserved boundaries

Remote PR #8 was open and unmerged on 2026-10-26 KST, with exact HEAD
`78e927b7712a9333a59ff99c36c4e7e8e359d239`. This additive branch and its stacked PR
use that commit. `main` was `2bbe54d3b9d0797ed4027ad95cdef1461ce58e59`.
All inherited tracked files, code, freezes and results remain unchanged.

The supplied [design contract](ECONOMIC_RESEARCH_V4_PROTOCOL.json) is retained
verbatim with its original `DESIGN_NOT_IMPLEMENTED` status. It describes the
contract at delivery, not this implementation's status. This document and the
[runbook](ECONOMIC_RESEARCH_V4_CODEX_RUNBOOK.md) state the implemented scope.

PR8 `value_evidence.EvidenceStore` is reused by subclassing: its receipt writer,
JSON decoder and hash utilities are retained. `VersionedEvidence` replaces its
network/cache path with a refresh-by-default collector, explicit reviewed hosts,
pre-request checks on **each** redirect, an immutable version index and durable
per-request journals. No global monkey-patching of the legacy allowlist occurs.
Source bytes and journal files are fsynced/atomically replaced before SQLite
indexing; restart imports committed receipts with their original receipt time.
An interrupted raw-file write without a durable receipt is not admitted as an
observation. The legacy JSONL receipt mirror is retained for compatibility; the
individual journal files, not a possibly partial trailing JSONL line, are the
new adapter's recovery authority. One capture writer per workspace is required.

## Working path

`init → collect-once → ingest → packet → register → report → resolve → compare`
is implemented in `one_trading/economic_research/`. `audit` checks storage,
raw bytes, receipts and packet manifests. The CLI is explicit and one-shot.

- `store.py` / `schema.sql`: append-only SQLite envelopes, entity content hashes,
  IDs, transactions, actual UTC storage timestamps and rejected-attempt records.
  The envelope is generic; domain functions validate entity fields and references.
- `evidence.py`: raw snapshots, receipt recovery, source controls, exact excerpt
  or JSON-pointer locations, epistemic types, Decimal derivations and public spot
  tickers. A copied old publication never gets a backdated `observed_at`.
- `events.py`: canonical chain/contract identity, explicit migration/bridge
  links, economic-event revision transitions and separate flow ledgers.
- `theses.py`: frozen packet closure over identities, event/flow versions,
  observations, sources and prices; thesis/forecast/decision registration.
- `valuation.py`: joint or conditional scenarios, unit/currency/cost/horizon
  validation, expected return, downside, break-even/required-return entry prices
  and price sensitivity. All monetary arithmetic uses Decimal.
- `review.py`: resolution revisions, resolved-only Brier/MAE and calibration
  summaries, attribution, frozen challengers and paired future policy diagnostics.
- `render.py` / `cli.py`: offline JSON/Markdown report regeneration and command
  manifests containing base commit and local code-content hash.

The reasoner backend is **manual_import**. Codex reads the packet and writes the
analysis JSON; Python validates and records it. `model_call_status=NOT_RUN` refers
to the absence of a model API call by this Python program. It does not mean that
the human-facing Codex session did no analysis. There is no unattended Python
reasoner, scheduler, paid service or deployed process.

## Time, revision and accounting rules

The enforced prospective boundary is
`input.available_at <= packet.as_of == forecast.knowledge_cutoff <= registered_at`.
Registration/decision timestamps come from the storage operation. Later evidence
requires new records. A new decision may reference a later price, but that price
must have been available before that new decision; observed prices must be no
more than one hour old. Past packets explicitly reference old content versions.
The one-hour freshness limit is an implementation setting, not a trading rule.

The first forecast and amended forecasts persist under one question identity.
Its deadline and resolution rule cannot be rewritten. Reports default to INITIAL
revision scoring; the review API also supports LATEST_AS_OF. Both preserve the
raw revision count. Source-claimed dates, effective dates, actual receipt,
parsed availability, registration and due dates remain separate. Historical
reconstruction modes are labeled and excluded from prospective scoring. Famous
historical outcomes known to the model are not clean prospective evidence.

Observations distinguish DIRECT_RECORD, SOURCE_CLAIM, DERIVED_VALUE and
ASSUMPTION. Numeric literal observations must match their source locator; derived
SUM/SUBTRACT values are recomputed and retain the input epistemic types. This
does not establish that an author's numbers are true. HTML semantic claims and
period attribution still require analyst review. No document instructions are
executed. Unknown values and unobserved flow categories are null.

Supply accounting keeps issuance/burn separate from unlock, redistribution,
relocking, market purchases and bridge representations. A market purchase and
its later burn do not create two units of tokenholder economic benefit. A source
claim of burn permanence is not independent chain verification. Different assets
cannot be summed as one supply ledger. Flows and semantic forecast questions
have duplicate checks, in addition to transaction/import idempotency.

## Resolution and policy comparison

Registration accepts immature 7/30/90/180-day questions. Maturity gates scoring
and training-label extraction, never today's event collection or new decision.
No result means PENDING before the deadline, DATA_MISSING after it; conflicting
definitions can be explicitly AMBIGUOUS. A resolved record requires a source-linked
measurement, matching asset, unit, measurement identifier, original observation
window and preregistered source. A source claim is admissible only when the
forecast explicitly allows it. Null and ASSUMPTION cannot resolve a question.
Late evidence may resolve the original window; it does not move the deadline.
The analyst must verify effective-window evidence; the first version is not an
automatic chain-indexer or a proof of arbitrary economic causal attribution.

Report registered revisions, selected questions, due, resolved, unresolved and
declared independent-event clusters separately. Brier/MAE denominators contain
only resolved questions within the same type/horizon/revision policy. Missingness,
resolution delay and resolved-subset selection bias remain visible. Clusters are
reviewed identifiers, not statistical proof of independence.

Policies freeze causal, estimation and decision layers separately. A challenger
changes one major rule and specifies its benefit, cost, applicability, endpoint,
review, rollback and future start. At most one active challenger per mechanism is
accepted. Rejections/reviews are new records. Both policies must log decisions on
the same frozen inputs **after** preregistration. `compare` supports a resolved
one-unit terminal-price entry-versus-cash diagnostic and missed-positive-markout
calculation when a numeric price forecast matures. It refuses to turn a binary
price question into a portfolio return. The synthetic test covers pending,
resolved, attribution and rejection paths. There has been no real-market learning
or promotion from twelve still-pending questions.

## Execution boundary

All initial decisions are PROPOSED. They create no fill, cash, holding or PnL.
No approved paper execution configuration has been connected, so PAPER_APPLIED
and HOLD/ADD/REDUCE/EXIT are rejected. A future broker adapter must bring a real
paper portfolio snapshot and reconcile fills before enabling these states.
This narrower implementation fails closed instead of inventing holdings.

PR8/PR7's historical D+2, gap filters, stop rules, broker and account ledger are
**not connected** to v4 price markouts. A current entry date is not suppressed
because a 180-day outcome is immature. Neither the legacy ridge model nor
governance MEASURED/PF defaults certify this economic reasoning. Research-policy
records cannot change execution authority or risk limits; capital authorization
always remains false, including a descriptive CAPITAL_REVIEW state.

## Real source scope and current research decision

The private current run used official documents, selected governance threads,
official token lists, a bounded Aave Finance index and Binance public instrument/
ticker responses. This is a pilot, not a complete event census or onchain audit.

- **AAVE:** [program documentation](https://aave.com/docs/ecosystem/aave),
  [funding discussion](https://governance.aave.com/t/aave-dao-funding-insights/24192),
  [pause disclosure](https://governance.aave.com/t/arfc-pause-aave-buybacks/24686),
  [later funding proposal](https://governance.aave.com/t/direct-to-aip-may-june-2026-funding-update/25000)
  and a community restart thread. General program language does not prove current
  execution. The later funding proposal discusses GHO runway; it is not proof of
  renewed AAVE market buying. Purchased tokens may be redistributed. Current net
  market purchases remain unknown. Exclude the continuing-buyback investment
  premise until current flows establish it; preserve a resumption hypothesis.
- **UNI:** [fee architecture](https://developers.uniswap.org/docs/protocols/protocol-fee/overview),
  [new Aggregator Hooks announcement](https://blog.uniswap.org/aggregator-hooks-are-now-live)
  and [Arc expansion](https://gov.uniswap.org/t/temp-check-protocol-fee-expansion-arc/26287).
  The new routing path is the most specific recent research catalyst, but net
  incremental capture and canonical sink transfers were not measured. Bridge
  synthetic-token burns are not extra global supply reduction. Research first,
  wait on entry at the recorded price under the stated assumptions.
- **CAKE:** [tokenomics](https://docs.pancakeswap.finance/protocol/cake-tokenomics)
  and [September report](https://blog.pancakeswap.finance/articles/september-cake-burn-report).
  The report's mint-minus-burn arithmetic is checked as a calculation from author
  claims. It excludes recycling and prorates weekly burns; it is not independently
  reconstructed net issuance or open-market buying. Its two burn subcategories
  sum to one token less than the reported total, another reason to reconcile
  underlying records. Continue observation; do not turn a deflation target into
  an expected investment return.

All probabilities, terminal prices, terminal economic supply and the common 30%
180-day hurdle in the current cards are **judgment assumptions**, not calibrated
estimates or an optimized strategy. Joint scenarios keep correlated outcomes
together. Buy/sell costs are diagnostic assumptions, not approved broker settings.
Cash is the enabled common benchmark. BTC relative performance is not enabled
without the initial benchmark observation. The three candidates share crypto/
DeFi exposure; their future outcomes will not be independent.

**Next single research action:** trace new UNI Aggregator Hook transactions into
TokenJar fee capture and canonical UNI sinks, then test whether net incremental
capture remains positive after replaced routes and incentives. If that
counterfactual cannot be identified, mark the economics question AMBIGUOUS rather
than claiming an effect from gross volume.

## Validation and limitations

`tests/test_economic_research_v4.py` names T01–T14 directly and adds numeric,
migration, atomicity, report reproduction and mature policy-path regressions.
They are synthetic software tests. Full repository checks and actual local
capture/audit/repetition are described in the runbook and completion report.

Still unvalidated: actual future outcomes, probability calibration, complete
chain-level supply/capture attribution, causal counterfactuals, live slippage,
position-path performance and profitability. No real orders, account connection,
continuous deployment, risk expansion or previous-PR merge is part of this work.
