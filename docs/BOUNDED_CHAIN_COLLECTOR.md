# Bounded Ethereum hook collection

This read-only, one-shot collector reuses PR8/v4 content-addressed bytes, durable
request receipts and the immutable research ledger. It accepts a frozen spec;
it does not choose a more active range after looking at outcomes.

```sh
python -m one_trading.economic_research.cli collect-chain-once \
  --workspace /outside/git/research-runtime --spec collection.json \
  --asset-id eip155:1:0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
  --output cycle-and-link.json
```

Use the actual registered asset ID in that workspace. The public specification
in `specifications/uni-collector-20261026.json` freezes identity, historical block
hashes, UTC range, completeness, finality and unit rules. Runtime files and
captured responses are private and deliberately absent from Git. The first run
captures a finite range, while the same spec's next run verifies its explicit
capture map offline. CLI invocation manifests increase per invocation; the
cycle, observation, event, packet and fee facts do not. A missing cycle is never
silently replaced with a later success. Further collection requires a new
explicit spec ID and preserves the old cycle.

Successful receipt status, exact HookSwap and ProtocolFeesCollected signatures,
and a one-to-one ERC20 Transfer from PoolManager to the declared TokenJar are
required for direct gross attribution. Each token retains its atomic integer
and separately queried decimals. Transaction hashes, receipt positions, block
hashes and log indexes trace observations to original bytes. Native currency
fee events remain unreconciled without traces. Failed transactions, allowance,
approvals and proposals are not executions. A fee fact's cross-cycle identity
is chain/block hash/transaction/log index; do not sum overlapping cycle totals.

Zero requires every receipt page to match the block's entire transaction set,
consistent indexes/parent hashes, finality, fixed endpoint hashes, unchanged
deployed code, route bindings and units. Provider errors, missing pages or
unconfirmed state produce DATA_MISSING/null, never a measured zero. This checks
consistency of a public provider's responses; it does not independently replay
Ethereum consensus or cryptographically prove an honest provider.

Shared TokenJar UNI sink transfers are context, not attribution to this hook.
No totalSupply reduction is inferred from a sink transfer. Gross fees do not
identify incremental net capture: replacement routes, incentives, conversion
times and a counterfactual remain unmeasured. Causal increment stays AMBIGUOUS.
The progress observation is a labeled SOURCE_CLAIM summary with a checked cycle
and raw inputs, not a numeric forecast outcome. No forecast, outcome, learning
label, execution or policy promotion is created by this command.

The separate CAKE question specification names four absolute UTC seven-day
intervals and a two-day tail. It is not a forecast or retrospective change to
CAKE-30d-r1. Future registration must preserve model/manual origin, policy,
target, original request/response, packet hash, cutoff, registration and exact
matching sentence/window timestamps. No probability is invented here.

Independent private-bundle commands (stdlib only, no ONE imports):

```sh
python scripts/verify_collector_bundle.py .
python scripts/recompute_chain_cycle.py runtime spec/collection.json actual-cycle.json
python scripts/verify_preserved_ledger.py .
```

Export all manifest names as POSIX relative paths. The verifier accepts legacy
backslash separators but rejects traversal, absolute/drive paths, escaping
symlinks and normalized duplicates. Export Git code with
`git -c core.autocrlf=false archive HEAD`; record the commit/tree and both Git
blob ID and exported byte SHA256. Never normalize market/document response
bytes. SQLite must be backed up via its backup API; the complete JSONL contains
all envelope fields, including sequence and actual recorded_at.

New synthetic tests cover only collector/packaging boundaries. The inherited
suite remains a software gate, not profitability or actual economic validation.

## Qualification and recovery (COL-01 through COL-04)

New measurements use `chain-qualification-v2`. Candidate logs remain in the
cycle even when identity/route, finality, canonical membership or units fail.
Each candidate records these checks. Only qualified facts enter the fact ledger
and support EXECUTED. A valid positive transaction may supply an observed lower
bound despite a different missing receipt page; the range count remains null
and zero remains unproven. State checks refer to the frozen end block; this is
not a per-transaction EVM replay. Shared UNI context is never hook revenue.

Linking validates the registered chain, canonical UNI contract, representation
and decimals before any domain write and before returning an existing link.
Observation, fact, event, packet and link writes share one transaction. Retrying
also verifies and reuses valid partial rows left by the older non-atomic writer,
including a packet persisted before interruption. Related events exclude self.

Old cycle bodies retain the legacy measurement algorithm and original clocks.
To append an offline reassessment without replacing them:

```sh
python -m one_trading.economic_research.cli review-chain-cycle \
  --workspace /outside/git/research-runtime --cycle-id EXISTING_CYCLE_ID \
  --output cycle-review.json
```

The review identifies excluded legacy events and fee facts. New packets exclude
events quarantined by reviews available at their cutoff; historical packets and
forecasts stay intact. Invalid existing links are rejected instead of silently
reused. A review is idempotent; CLI execution manifests record each invocation.
The independent calculator first binds supplied JSON to the ledger, then
reconstructs full fee, swap and shared-transfer fields from raw responses. Its
raw comparison remains necessary even when a corrupted export's JSON and DB
body agree. The regression suite tests both attacks on disposable synthetic
exports without weakening production append-only triggers.
