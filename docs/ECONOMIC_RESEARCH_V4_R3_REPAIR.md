# v4.3 raw locations and explicit legacy question recovery

R3-01 and R3-02 were reproduced on `02edfc9275f7481c9b6ea08f7031f24d9deb0b2e`.
The supplied 35 cases contained 33 passing controls and two defect demonstrations;
their historical exit code 0 was not acceptance. The separate legacy producer is
the supported API at `e140232eddd2993799e7795fd2f18f4c7a450ac0`.

## Raw fact identity

`locations.py` resolves the original bytes to typed object keys and array indices.
JSON pointer escapes are validated, and array indices accept only `0` or a decimal
integer without leading zeros. Object key `"00"` remains legal. Two distinct leaves
containing the same value remain different facts. JSON duplicate keys are rejected.

Numeric JSON evidence must use a pointer. Numeric substring quotes are rejected,
rather than guessing the leaf. Non-JSON quotes retain every matching trimmed byte
span as shared provenance: repeated HTML/embedded content is not independent
evidence. Partial numeric tokens are rejected. A quote with surrounding whitespace
and its trimmed alias have the same spans. Missing values and the display citation
of a derived calculation are not numeric measurements; derived **inputs** still
undergo the full numeric provenance gate. Missing/assumed facts cannot become labels.

### Complete numeric tokens (R4-01)

Numeric non-JSON quotes must cover a complete token. For example, `1e1` is the
single value 10; its exponent `1` cannot be registered as another measurement.
The parser accepts ASCII signs, integers, decimals, scientific notation and valid
three-digit comma grouping. Malformed grouping, underscores, unsupported Unicode
number spellings and non-UTF-8 numeric source text fail closed. It preserves exact
UTF-8 byte positions, including when Korean text surrounds the number. TEXT quotes
retain their existing literal substring behavior.

This check also runs when auditing and scoring saved observations. Previously
accepted partial-token observations remain immutable audit issues; they cannot
supply a score or training label until a valid resolution explicitly supersedes
the affected resolution. `tests/test_economic_research_v4_r4.py` covers both new
input and preserved legacy contamination. This repair does not relabel or delete
old records.

`semantic-fact-v3` combines these raw positions, original byte digest, exact numeric
identity and UTC measurement interval. It never changes a saved observation body.
Derivations reject overlapping leaf fact sets. Events, stored-resolution audit,
scoring and training export recompute the same identity. Legacy invalid aliases
remain audit issues. A valid new locator can be registered without the invalid
alias vetoing it; matching raw bytes and value conservatively retain the earlier
parsing time. Corrections to resolved outcomes use `supersedes_resolution_id`.
Old invalid resolutions remain visible; scoring fails until a valid correction.

## Explicit legacy review

Only missing-target legacy questions in a multi-asset thesis may be quarantined.
All other contract checks must pass. Valid questions, including failed predictions,
cannot be quarantined. Quarantine records retain original forecast body hashes,
the missing-target reason, analyst review reason and actual registration time.
Original envelopes and outcomes are never rewritten or assigned a guessed asset.

Use the existing offline ingest CLI with a JSON array. First explicitly review
**each** ambiguous question blocking the ledger:

```json
[{"kind":"question_quarantine","data":{
  "quarantine_id":"review-old-price","question_id":"old-price",
  "review_reason":"Legacy A/B context did not identify a measured asset"
}}]
```

Then register the replacement via `question_recovery`, whose `forecast` is a full
normal forecast payload with a **new** question/forecast ID, `supersedes_forecast_id:
null`, and explicit `target_asset_id`. Example outer payload (the forecast object
must include all the usual required fields):

```text
[{"kind":"question_recovery","data":{
  "recovery_id":"link-old-price-to-A","quarantine_id":"review-old-price",
  "review_reason":"Analyst explicitly selects A; old target remains unknown",
  "forecast": <complete new explicit forecast>
}}]
```

```sh
python -X utf8 -m one_trading.economic_research ingest --workspace /outside/git/runtime --file review.json
python -X utf8 -m one_trading.economic_research audit --workspace /outside/git/runtime
python -X utf8 -m one_trading.economic_research report --workspace /outside/git/runtime
```

Recovery appends a link and a forecast atomically, and replays idempotently. It
preserves the original endpoint, measurement window and deadline. A known original
outcome, retrospective history, expired deadline or already-known admissible raw
outcome cannot become a new prospective success. A direct unlinked replacement
matching a quarantined contract is rejected. An unrelated valid forecast can be
ingested with kind `forecast`. Valid duplicate-contract, immutable-question and
per-policy parent/clock checks remain active. Unreviewed ambiguity still fails
closed; a quarantine is never silently inferred.

Reports show quarantine counts, reasons, excluded forecast IDs and recovery links.
Quarantined records contribute no accuracy/training labels. The total registration
count still includes them. Audit continues to report **ISSUES** for old ambiguity,
even after successful recovery. Past `as_of` reports cannot use future quarantine
records. The procedure is not an erasure of historical performance.

## Verification and limits

The provided 33 control case functions and old producer function remain unchanged
in the private acceptance bundle. Only the two locator defect expectations and
the legacy consumer's blocked-registration expectations are replaced. The same
old API produces 23 legacy records: recovery preserves all 23 envelopes and adds
three quarantine records, one recovery link and two forecasts (29 total).

`tests/test_economic_research_v4_r3.py` covers strict paths, distinct leaves, HTML
overlap, contaminated legacy resolution/label rejection and correction, recovery
atomicity, immutable originals, visible audit issues, duplicate protection,
valid-history quarantine rejection, known outcomes and expired questions.

Run the complete suite, compilation and CLI checks from the repository root:

```sh
python -X utf8 -m unittest discover -s tests -v
python -X utf8 -m compileall -q one.py one_trading tests scripts tools
python -X utf8 one.py --help
python -X utf8 one.py status --workspace workspace_public
python -X utf8 -m one_trading.economic_research --help
```

Real runtime and source bytes remain outside Git. Software acceptance, byte/ledger
preservation and future investment performance are separate conclusions. Pending
predictions have no accuracy denominator. No orders, paper fills, deployment,
policy promotion, or PR merges are implemented by this repair.
