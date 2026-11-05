# Model transport evidence and one-shot pilot

Follow-up to PR23. Previous paid-pilot-20261105-01 originals remain frozen.
HTTP failure evidence contains status, bounded machine error type/code, server request ID, generated client request UUID, request/response timestamps and completion classification. Arbitrary messages, response bodies and authorization headers are not logged. Error reads are capped at 8193 bytes; labels at128 characters. A malformed/truncated/5xx/timeout response remains UNKNOWN_COMPLETION, never a confirmed zero bill. No retries.

Any failed or rejected model attempt stops the capture immediately after fsync of the PAPER journal. An intentionally interrupted source may lack a terminal record or two frames; independent full-run audit is INCOMPLETE, not PASS. All originals remain available. Full source consistency PASS is only possible with a complete source journal.

New transport auditor independently checks request identities, timestamps, duplicate client IDs and completion semantics; source/PAPER auditors and causal future-quote fill guards remain separate. Fees and usage-priced model expense remain economic costs even when paid by credits; credit drawdown and cash billing require separate provider billing evidence.

Preflight may read model metadata without generating tokens. A model-list 403 does not establish generation access or billing organization: restricted keys can have different read/generate rights. Unverified organization, credit balance and provider spend caps must be disclosed. The approved one-shot first generation is the actual access test. If it fails, no second generation is allowed.

Validation: python -m unittest discover -s tests -v; python -B -X utf8 scripts/run_observer_audit.py --out <new-private-folder>. Actual evidence is private, never a public CI artifact.
