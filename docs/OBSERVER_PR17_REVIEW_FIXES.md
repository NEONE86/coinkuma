# Official review counterexamples and corrections

The official Codex review of publication `2f1161d2586324957c6461bfba6255fb616b0d3f`
reported three findings. All three were independently reproduced with synthetic
inputs before changing the implementation: three tests failed, not skipped.
No actual market data was collected during reproduction or repair.

| Finding | Before | Correction and controls |
| --- | --- | --- |
| Substituted normalized input/raw body | A rehashed ledger with price 100 replaced by 200 passed the independent verifier while the original receipt stayed unchanged. | Bind immutable input fields to its receipt and normalize from that receipt's raw hash. Rehashed fabrication is rejected with `INPUT_RECEIPT_BINDING`; original export passes. |
| One approval reused in a different runtime | The same one-attempt approval admitted one HTTP intent in each of two fresh runtimes. | Require approved run ID and canonical runtime path; persist the approval digest and ledger instance in a separate local claim registry. Different targets and a recreated same-path ledger are rejected; same-instance restart retains its consumed attempt. |
| Pending buy after a FUTURE mini | An ENTER still filled after a future-dated mini arrived before its book. | At execution, check current mini/bar eligibility and bar freshness, with the existing entry liquidity threshold. Valid input and unrelated-symbol updates still allow the existing fill; invalid current data rejects the pending buy. |

All reproductions use `tests/test_sentinel_review_regressions.py`; the expanded
suite also covers normal buys, other symbols, a future newer bar,
legitimate restart, recreated ledgers, changed targets and legacy approval
rejection. Existing P1/P2, bounded pending releases and bar-order cases remain in
`scripts/run_observer_audit.py`. No policy threshold changed.

Reproduce the three original failures in a separate checkout of the initial
publication by copying the regression module from commit `7004ce8` into its
`tests/`, adding that directory to `PYTHONPATH`, and running from that checkout.
That version predates the account-claim fixture helper and needs no new imports:

```sh
python -m unittest -v \
  test_sentinel_review_regressions.ReviewCounterexamples.test_fabricated_input_raw_is_not_an_original_receipt \
  test_sentinel_review_regressions.ReviewCounterexamples.test_one_approval_cannot_reset_http_budget_in_another_runtime \
  test_sentinel_review_regressions.ReviewCounterexamples.test_future_mini_after_enter_prevents_pending_buy
```

The module's `ROOT` points to its repository parent. The normal final-head
command is:

```sh
python -B -X utf8 -m unittest discover -s tests -p test_sentinel_review_regressions.py -v
python -B -X utf8 scripts/run_observer_audit.py --out /outside/repository/new-audit
```

The actual stopped trial is separate: its 3,182 original ledger records passed
the strengthened receipt-binding verifier, with zero network calls. Its exact
execution source, archive SHA256 and originals are preserved; it had no PAPER
decisions or fills. New approval requirements are not backdated into old records.
An updated development source is not authorized to collect under the old
approval. The original archive's embedded verifier is preserved for provenance;
the strengthened trusted verifier in this PR supplies the additional check.

The follow-up review on `7004ce8` added a cross-parent variant: changing the
approved runtime to a different parent also bypassed the original per-parent
claim registry. Its regression failed before correction. Claims now use the
operating account's fixed `~/.one-observer/approval-claims.sqlite3`, independent
of runtime or checkout. Test-only mocks redirect claims to temporary storage;
no actual account claim store is written during tests. The additional control
rejects reuse across parents as well as sibling and recreated runtimes.
