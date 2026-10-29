# PR14 follow-up

The operational adapter settles durable GROWTH receipts before checking expired
registration deadlines. Expired intents are closed without editing their deadline.
Unknown interrupted claims consume their full reservation; only positions still
open can receive a new maintenance intent. A recovered minute is cached to avoid
duplicate observations and charges. Authority guards and sealed Alpha code remain
unchanged.

Missing MATCHED quote acquisition has three durable slots, spaced 120 seconds
apart, and the original decision deadline. Exhausted or expired diagnostics remain
missing, with no zero-return replacement. This is separate from model retries.

The scheduler wrapper records sanitized executable preflight results. Windows
Start-Process on the actual host dropped the process-only PATH addition before
starting Python. The adapter now resolves the same pinned executable inside the
Python process that calls the sealed worker. No auth file is inspected or moved.
Version subprocess success is separate from an inference response. Existing
expired cases are never reopened to demonstrate connectivity.

Run from the repository with the existing authorized runtimes:

```powershell
python -B -m unittest discover -s tests -v
python -B -m one_trading.economic_batches --runtime <batch-runtime> alpha-service
python -B -m one_trading.economic_batches --runtime <batch-runtime> growth-service
python -B -m one_trading.event_attribution --runtime <batch-runtime>
```

The attribution command registers a new diagnostic with original trial hashes,
reads their six archived payloads, calculates four matched-clock alternatives at
six fixed horizons and acknowledges measured CPU time under the shared authority.
Re-running returns the existing result and acknowledges idempotently. It allocates
no alpha and makes no HTTP/model calls. Existing FAILURE_ATTRIBUTION specifications
retain their original semantics and due times.

Two immutable follow-up designs distinguish horizon decay from weak asset
selection/common market exposure. Their fields include unused future signal
boundaries, actual receipt requirements, 4h/24h matched entries, costs, cluster
handling, rejection criteria and termination. They are POST_HOC_GENERATED and
await separately registered collection; they do not enable a new collector.
The already running original scheduler continues its existing queue and Alpha
cohort. Do not describe these two waiting designs as enabled automated experiments.

Review-bundle commands (outside the repository, no project imports required):

```sh
python first-batch/verify_economic_batch_bundle.py first-batch
python latest/verify_research_transitions.py latest
python latest/verify_event_attribution.py latest
python verify_alpha_export.py latest
```

The original PR12/13/14 archives and before/after synthetic recovery JSON accompany
the delivered bundle. All archived signals remain TRAINING_OR_EXPLORATORY. Fixed
horizon markouts are post-hoc diagnostics, never a profitability validation.
