# Development guidance

Read README.md and docs/DATA_BOUNDARIES.md before changing code.

- Preserve chronology, matured labels, delayed execution, stale-plan identity
  checks, cost accounting and persistent-ledger idempotency.
- Run `python -m unittest discover -s tests -v` and the checks in docs/TEST_SPEC.md.
- Keep strategy changes separate from the recovered code-only import.
- The missing historical archive is intentional. Do not invent replay parity or
  describe synthetic tests as market evidence.
- Do not commit collected prices, runtime records, secrets or private identifiers.
- There is no exchange order/withdrawal implementation or deployed server.
- Repeat tuning on used data is not untouched out-of-sample validation.

## Observer delivery is a pull request

- Every Observer implementation, fix, audit tool or execution-helper change must
  end in a pushed review branch and a GitHub PR, not only a local report/ZIP.
  Stack on the latest relevant unmerged PR; never merge another PR implicitly.
  An explicit current user prohibition on publishing takes precedence; report the
  exact remaining publication blocker instead of claiming delivery is complete.
- Run `python -B -X utf8 scripts/run_observer_audit.py --out <new-outside-git-dir>`
  and inspect `Observer audit` on the exact submitted head. A pending/failed CI
  run is not a pass. Keep independent raw/ledger recalculation and semantic
  negative controls even when Codex reports no review findings.
- Real Observer receipts, raw bodies, ledgers, SQLite files and evidence ZIPs go
  only to the separately verified private evidence repository. Confirm remote
  visibility is PRIVATE immediately before upload, verify the stored SHA256, and
  link the approved run ID, execution source SHA256, archive SHA256 and audit
  status from the public PR. Never send private evidence or its credentials to
  a public PR job, Actions artifact, log or bot review comment.
- Request the official `@codex review` after creating the PR and after material
  corrections unless an automatic review for that exact head is already visible.
  Report requested/acknowledged/completed separately. Do not equate a posted
  request with enabled repository-wide automatic review or a completed review.
- A real-data collection still needs its own explicit, exact source/config and
  budget approval. PR/CI/review authorization does not activate models, market
  collection, notifications, real orders, scheduler changes or deployment.

## Code Review Rules

- Preserve original receipt time/bytes and first interpretation. A delayed
  release becomes available later; it must not backdate information or replace
  a newer symbol's closed bar by receipt/release order. Compare raw kline times.
- Bind approval, source/config identity, monotonic session, execution delay and
  every HTTP/WS attempt/uncertain reservation. Reject hidden quota resets,
  reused approvals, invented fills and immature-score promotion.
- Keep the public/private evidence boundary. Public CI may generate synthetic
  ledgers; it must never download actual evidence or expose a private token to
  PR-controlled code. Independent recomputation must reject rehashed semantic
  tampering, not merely verify a checksum or trust a claimed result.
