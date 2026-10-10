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
