# Observer bounded release and epoch EXIT correction

Local follow-up to e4e7c9e. No strategy, account, transport allowance, scheduler,
notification, model or order activation changes.

`clock_pending` accepts only structurally valid boundary UNCERTAIN items backed
by a contemporaneous calibration. Its active SQLite projection is limited to
512 items / 4 MiB of distinct envelopes / 5 seconds in the original monotonic
epoch. The append-only ledger retains every original and terminal outcome.
Tick and calibration updates may append `interpretation_release` and a distinct
input, with receipt and initial-interpretation links. Receipt time never changes;
new availability and execution delay start after normalization. Old REAL_REUSED,
FUTURE, INVALID, expired sample, clock jump and session changes cannot be promoted.

An EXIT rejected for NEW_MONOTONIC_EPOCH may create a linked retry only while
STRUCTURAL_INVALIDATION or HORIZON_EXPIRED still holds. There are at most three
such generations per position. New decisions recheck current inventory and risk,
owe their own 2-second delay and TTL, and still use the existing book/cost guards.
The parent decision/execution hashes are bound in `decision_retry`. A stale
STOP_LOSS is not a reason for this special retry. Cap exhaustion records a blocker.
No source/config guard is bypassed to reopen an older runtime.

Run from this repository with Python 3.13 (standard library):

```text
python -B -X utf8 scripts/run_sentinel_offline_tests.py --log observer.txt
python -B -X utf8 scripts/run_sentinel_offline_tests.py --pattern "test*.py" --log full.txt
python -B -X utf8 scripts/replay_pending_epoch.py --out NEW_EVIDENCE_DIRECTORY
python -B -X utf8 scripts/tamper_pending_epoch.py --evidence NEW_EVIDENCE_DIRECTORY --out NEW_TAMPER_DIRECTORY
python -B -X utf8 scripts/replay_sentinel_corrections.py --original ORIGINAL_ACTUAL_DIRECTORY --out NEW_LEGACY_CASE_DIRECTORY
python -B -X utf8 scripts/benchmark_sentinel.py --out NEW_LOAD_DIRECTORY --seconds 3600
python -B -X utf8 scripts/verify_observer_followup_bundle.py EXTRACTED_REVIEW_BUNDLE
```

The test runner uses unittest discovery and denies external sockets. Windows
stdlib socketpair self-pipes remain available solely inside socketpair calls.
The replay scripts use synthetic bytes or separately labeled old real receipts;
they do not collect markets. Freshness, costs and chronology are independently
recomputed without importing the implementation. Rehashed negative controls
include backdated release, deleted initial interpretation and overwritten parent
execution. The workload's 8 GiB cap is only in its separately labeled synthetic
fixture policy; production and proposed 5-minute approval caps stay unchanged.

This verifies offline behavior, not live liveness, 24-hour operation, notification
delivery, market performance or profitability. A new hash-bound 300-second public
trial requires its own approval; start, stop and approval reference stay null.
