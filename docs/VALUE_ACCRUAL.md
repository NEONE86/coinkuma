# Token value-accrual evidence workflow

Read [the conclusion first](experiments/value-accrual-2026-10-25/RESULTS.md):
**NEEDS_MORE_EVIDENCE**, no validated edge or live eligibility. This additive
study starts at PR #7 HEAD `35bfbbc2a71aa47579974d15e9d1c62e3ba7db55`.
All files inherited from that commit remain byte-for-byte unchanged.

## Clocks and storage

- `published_at`: exact onchain description/CID commitment inclusion, or London
  activation. It is not necessarily the earliest public offchain announcement.
- `first_seen_at`: first durable receipt of these exact bytes by this collector,
  in UTC. Every historical collector observation remains unknown/null.
- `executed_at`: proven economic configuration execution. Governance dispatch,
  completed buyback, allowance, sink transfer and burn are distinct. Unknown
  destination-chain execution remains null.
- Reconstructed availability is publication + fixed one hour, followed by D
  completion and D+2 earliest hypothetical entry. Strict collector availability
  also requires actual receipt/proof completion. It produces zero matured past
  observations in this snapshot.

Exact source bytes and append-only request receipts stay in a separate local
evidence directory. Public documents contain reviewed derived evidence and hashes,
not raw HTTP records, source webpages, market prices or simulation ledgers. SHA256
proves integrity, not source authenticity. Two explorer responses are checked;
no local consensus or Merkle inclusion proof is claimed. Sources are public GET
endpoints from an allowlist. No API keys, account APIs, orders or paid endpoints.

## Reproduce the supplied frozen snapshot offline

Python 3.10+ and the standard library are sufficient. Supply the original
evidence archive and PR #7 atlas/database retained outside Git. The public checkout
alone runs synthetic tests, but cannot recreate historical market evidence without
these inputs. An online refetch today may produce changed source bytes and cannot
replace the archived first-seen receipts. Use fresh output directories.

```sh
python -m unittest discover -s tests -v
python -m compileall -q one.py one_trading tests scripts tools
python one.py --help
python one.py status --workspace workspace_public

python scripts/run_value_event_study.py --evidence /data/event-evidence --atlas /data/OPPORTUNITY_ATLAS.jsonl.gz --freeze docs/VALUE_ACCRUAL_FREEZE.json --output /data/event-study-new
python scripts/validate_value_events.py --evidence /data/event-evidence --output /data/event-study-new --atlas /data/OPPORTUNITY_ATLAS.jsonl.gz --archive /data/market.sqlite3
python tools/reproduce_value_events.py --evidence /data/event-evidence --original /data/event-study-new --atlas /data/OPPORTUNITY_ATLAS.jsonl.gz --destination /data/event-reproduction-new
```

The validator independently decodes source text/CID commitments, compares original
receipts, checks first-seen times, reconstructs episode/maturity rules, reselects
controls and recomputes linked paths with PR #7's alternate label implementation.
The isolated reproduction makes a separate copy, runs every source/panel stage
with `--offline`, then compares eight substantive files byte for byte. Run time
fields differ and are explicitly excluded. This is a same-author audit.

## Collect a separate new snapshot

These commands read public network endpoints and create local files. They are
research tools, not a scheduled monitor. Do not reuse the historical freeze for a
changed panel or overwrite the original evidence directory. The cache is for
reproducing one snapshot; create a fresh directory for a new observation batch.

```sh
python scripts/collect_value_events.py --output /data/new-event-evidence
python scripts/resolve_value_events.py --evidence /data/new-event-evidence
python scripts/resolve_value_events.py --evidence /data/new-event-evidence --classifications docs/VALUE_ACCRUAL_CLASSIFICATIONS.json
```

New keyword candidates require explicit source-only classification before any
price outcomes are opened; the builder fails on an unclassified candidate rather
than silently admitting it. A new research run must receive its own protocol,
classification review and freeze. Current Aave file-path coverage is incomplete;
historical source folders are recovered from retained diff introduction commits.
Only small single-node UnixFS CIDv0 documents are supported; an unsupported CID
cannot be admitted as verified.

PR #7's account/strategy code and freeze remain unchanged. Its freeze hashes all
Python files in three directories, so adding this study means its whole-directory
hash set no longer matches in this checkout. To reproduce PR #7 accounts, use its
exact HEAD in a separate checkout; do not edit its old freeze to include new files.

## Preserved commitments

Protocol remote commit `06d73b1a7e1e390ef54a75f596d4edb62cb6f3c9`, then tested
code/panel remote freeze `5951fc60df487c939c30c508b1e4536d3a4365cb`, both preceded
the new event-price linkage. [Freeze](VALUE_ACCRUAL_FREEZE.json) records LF code
hashes and exact panel/atlas byte hashes. The public report's artifact hashes refer
to LF publication bytes; raw evidence hashes refer to exact captured entity bytes.
No empirical parameter search or post-outcome strategy change occurred.
