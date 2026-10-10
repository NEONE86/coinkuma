# Public export scope and lineage

Prepared 2026-10-10 from the recovered ONE Trading v0.3.1 working baseline.
The lineage includes the v0.2 observation/research code and the v0.3.1 learning,
portfolio, replay and forward-paper implementation, plus the available governance
module and its tests. This statement is descriptive provenance, not an external
signature or independent attestation.

`one.py`, `one_trading/*.py`, `tests/*.py` and `config.json` are copied byte-for-byte
from the reviewed recovered baseline. No strategy thresholds, model coefficients,
execution rules or existing tests were changed for this export.

New public packaging consists of this README, reviewed documentation, ignore rules,
a Python version hint, developer instructions and an offline-test CI workflow.
The private recovery's database-dependent CI and historical verification scripts
are not included because their inputs and original Git ancestry are not public.

## Explicit exclusions

- All saved market-price databases and raw collected API snapshots.
- All historical trade, fill, equity, model-decision and model-audit records.
- All network probes, connection-failure records and execution/test audit logs.
- Prior reports, dashboards, manifests and private recovery provenance metadata.
- All archived bundles, ZIPs, private source-history commits and tags.
- Credentials, secrets, account information, environment files and runtime state.
- Private artifact identifiers and local machine paths.

Only the existing public repository's initial placeholder commit is used as the
parent of the export commit. No private historical commit is a parent, and no
private Git objects are uploaded. The branch is offered as a draft pull request;
publication does not imply merge, deployment, or approval for real trading.

Synthetic in-test records and virtual research defaults are intentionally included
so the standalone test suite remains runnable. Generated local records are ignored
by Git. Ignore rules are an accident-prevention measure, not a secrets detector.
