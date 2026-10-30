# Observer delivery and evidence boundary

Observer work is delivered through a GitHub PR. Local test reports and evidence
ZIPs supplement that PR; they do not replace a reviewable published change.
The root AGENTS.md and Observer PR template make this the default for Codex work.
Existing source-frozen runtimes are never repointed to a new PR automatically.

## CI on every change

`.github/workflows/observer-audit.yml` runs on push, pull_request and manual
dispatch, without a path filter. Its offline job uses Linux/Python 3.12 and
Windows/Python 3.13. It runs existing Observer regressions, creates eleven fresh
pending/epoch and four bar-order synthetic cases, independently recalculates the
ledgers and bar projections, then rejects three rehashed pending/epoch tampers
and two bar terminal/index tampers. Existing generic CI continues the full suite.

Only the explicitly synthetic summary is uploaded as an Actions artifact.
No API key, private repository token, model, live adapter, notification channel
or actual market dataset is supplied. The workflow uses contents:read and does
not use pull_request_target. Independent verifiers remain separate from the
Observer implementation.

Run the same offline path locally from the repository root:

```sh
python -B -X utf8 scripts/check_observer_publication.py
python -B -X utf8 scripts/run_observer_audit.py --out /outside/repository/new-audit
python -m unittest discover -s tests -v
```

CI checks apply to the submitted branch immediately. They become the default
workflow for other branches only after a maintainer merges the delivery PR.
No merge or branch-protection change is implied by this task. AGENTS.md is agent
guidance; it is not a server-side prohibition on direct pushes.

## Private actual evidence

Use `NEONE86/coinkuma-observer-evidence` only after verifying the repository is
PRIVATE and belongs to the intended account. No automatic fallback to coinkuma
or a public asset is permitted. Keep the full approved-run ZIP as a private
release asset, verify the stored asset's digest/size against the local original,
and retain the original locally. Do not overwrite a previously published asset.
Grant no new collaborators or broad access implicitly.

The public `docs/observer/runs/` registry contains only allowlisted identifiers,
hashes, archive size, private release pointer, audit status and limitations. It
contains no raw payload, ledger, model answer, account state or authentication.
An AWAITING_PRIVATE_STORAGE reference is explicit incomplete handoff; the
separate evidence-handoff CI job fails until a verified private link is recorded.
Public CI validates the pointer schema; it cannot independently authenticate
the private repository or re-download the asset. That requires an authorized
operator and is recorded at the publication step. A hash is not source
authenticity or proof of investment advantage.

Actual data verification is performed in an authorized private/local environment
with the frozen executed source and the archive's standalone verifier. Never
checkout arbitrary public-PR code next to a private credential or original data.
New runtime observations keep their approved original source hash even if the
publication commit includes later audit tooling. CRLF execution bytes and Git
blob identity must not be conflated.

## Official Codex review

Use repository/personal automatic-review settings when available, or request
`@codex review` in the PR conversation. Check for the bot's acknowledgement and
actual review, and re-request after material fixes when required. A comment is
a request; it does not prove the setting is enabled or a review completed.
Do not add an API-key-powered imitation review workflow.

Official setup and triggers:
https://learn.chatgpt.com/docs/third-party/github

Codex is an additional reviewer. It cannot replace the independent ledger
recalculation, malformed-input cases, tamper controls, human approvals or source
and budget constraints. Creating a PR never authorizes new collection or orders.

## Frozen real-trial audit tools

`scripts/observer_trial_audit_v1/` publishes the three unchanged, read-only audit
tools from the approved 2d67741 evidence package. They are specific to that run's
execution identity. With an authorized local copy of the private package:

```sh
python -B -X utf8 scripts/observer_trial_audit_v1/verify_package.py /private/extracted-bundle
python -B -X utf8 scripts/observer_trial_audit_v1/tamper_trial.py /private/extracted-bundle --temp-parent /private/empty-audit-work
```

These commands do not collect market data. They verify the archived source,
original receipts, SQLite projection, transport reservations and explicit
approval. The negative controls alter temporary copies only. The bundle retains
the exact original audit scripts and launch provenance. This public checkout
contains no real receipt, authorization document, ledger, ZIP, or local path.
Run these commands locally with private evidence; never supply it to public CI.

The approved execution bytes remain SHA256
`00601caa13df0b19e7853f457715cadcfbef0882756dec26bdfba9bc17593338`.
The initial publication used the same implementation with different line endings.
Subsequent [Codex review fixes](OBSERVER_PR17_REVIEW_FIXES.md) change the new
development source. They do not modify the frozen executed checkout, original
approval, archive or runtime. This PR does not reuse an old approval to run a new
checkout. Any future collection requires a fresh exact source/config approval,
including its run ID and canonical runtime path.
