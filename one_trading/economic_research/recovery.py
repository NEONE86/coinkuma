"""Explicit append-only review of ambiguous legacy questions, never retargeting."""
from contextlib import nullcontext

from . import store as clock
from .contracts import question_contract
from .store import digest, instant, nonempty

AMBIGUOUS = "unambiguous_target_asset_id_required"


def _members(store, question_id):
    rows = [f for f in store.all("forecast") if f["question_id"] == question_id]
    if not rows:
        raise ValueError("unknown_quarantine_question")
    return rows


def _ambiguous(store, rows):
    for f in rows:
        subjects = store.get("thesis_revision", f["thesis_revision_id"])["subject_asset_ids"]
        if "target_asset_id" in f or len(subjects) < 2:
            raise ValueError("only_legacy_missing_target_may_be_quarantined")
        try:
            question_contract(store, f)
        except ValueError as exc:
            if str(exc) != AMBIGUOUS:
                raise
        else:
            raise ValueError("valid_question_cannot_be_quarantined")
        # Ensure the missing target is the only contract defect. This checks
        # candidate interpretations, without assigning one to the stored row.
        for asset in subjects:
            question_contract(store, dict(f, target_asset_id=asset))


def validate_quarantine(store, record):
    rows = _members(store, record["question_id"])
    _ambiguous(store, rows)
    if record["forecast_hashes"] != {f["forecast_id"]: digest(f) for f in rows} or record["reason"] != AMBIGUOUS:
        raise ValueError("quarantine_scope_changed")
    if any(instant(f["registered_at"]) > instant(record["registered_at"]) for f in rows):
        raise ValueError("quarantine_before_original")
    return rows


def quarantine_question(store, payload):
    if set(payload) != {"quarantine_id", "question_id", "review_reason"}:
        raise ValueError("quarantine_schema")
    nonempty(payload, *payload)
    rows = _members(store, payload["question_id"])
    _ambiguous(store, rows)
    if any(q["question_id"] == payload["question_id"] and q["quarantine_id"] != payload["quarantine_id"]
           for q in store.all("question_quarantine")):
        raise ValueError("question_already_quarantined")
    old = store.get("question_quarantine", payload["quarantine_id"], optional=True)
    record = dict(payload, reason=AMBIGUOUS, forecast_hashes={f["forecast_id"]: digest(f) for f in rows},
                  registered_at=old["registered_at"] if old else clock.utc(), status="QUARANTINED_REQUIRES_REVIEW")
    validate_quarantine(store, record)
    return store.put("question_quarantine", record["quarantine_id"], record)


def quarantines(store, as_of=None):
    result = {}
    for record in store.all("question_quarantine"):
        if as_of and instant(record["registered_at"]) > instant(as_of):
            continue
        validate_quarantine(store, record)
        result[record["question_id"]] = record
    return result


def validate_recovery(store, link, forecast):
    quarantine = store.get("question_quarantine", link["quarantine_id"])
    originals = validate_quarantine(store, quarantine)
    if (link["old_question_id"] != quarantine["question_id"]
            or link["new_question_id"] != forecast["question_id"]
            or link["old_question_id"] == link["new_question_id"]
            or "target_asset_id" not in forecast):
        raise ValueError("recovery_requires_new_explicit_question")
    contract = question_contract(store, forecast)
    for original in originals:
        subjects = store.get("thesis_revision", original["thesis_revision_id"])["subject_asset_ids"]
        if forecast["target_asset_id"] not in subjects or question_contract(
                store, dict(original, target_asset_id=forecast["target_asset_id"])) != contract:
            raise ValueError("recovery_must_preserve_original_endpoint")
    if link["contract_hash"] != digest(contract):
        raise ValueError("recovery_contract_changed")
    if instant(link["registered_at"]) < instant(quarantine["registered_at"]):
        raise ValueError("recovery_before_quarantine")
    mode = store.get("thesis_revision", forecast["thesis_revision_id"])["mode"]
    old_ids = {f["forecast_id"] for f in originals}
    if mode == "PROSPECTIVE" and (any(f["mode"] != "PROSPECTIVE" for f in originals) or any(
            r["forecast_id"] in old_ids and r["status"] == "RESOLVED"
            and instant(r["resolution_at"]) <= instant(link["registered_at"])
            for r in store.all("resolution"))):
        raise ValueError("recovery_cannot_reset_known_or_retrospective_history")
    return link


def check_new_question(store, forecast, contract):
    isolated = quarantines(store)
    if forecast["question_id"] in isolated:
        raise ValueError("quarantined_question_requires_new_identity")
    for prior in store.all("forecast"):
        q = isolated.get(prior["question_id"])
        if q:
            subjects = store.get("thesis_revision", prior["thesis_revision_id"])["subject_asset_ids"]
            if contract["target_asset_id"] in subjects and question_contract(
                    store, dict(prior, target_asset_id=contract["target_asset_id"])) == contract:
                links = [r for r in store.all("question_recovery") if r["quarantine_id"] == q["quarantine_id"]
                         and r["new_question_id"] == forecast["question_id"]]
                if len(links) != 1:
                    raise ValueError("explicit_recovery_link_required")
                validate_recovery(store, links[0], forecast)
            continue
        # Unreviewed invalid history still fails closed. Valid contract and
        # per-policy lineage rules are unchanged.
        same = question_contract(store, prior) == contract
        if prior["question_id"] == forecast["question_id"] and not same:
            raise ValueError("question_contract_immutable")
        if prior["question_id"] != forecast["question_id"] and same:
            raise ValueError("duplicate_forecast_question_use_revision")


def recover_question(store, payload):
    if set(payload) != {"recovery_id", "quarantine_id", "review_reason", "forecast"}:
        raise ValueError("recovery_schema")
    nonempty(payload, *payload)
    forecast = payload["forecast"]
    if forecast.get("supersedes_forecast_id"):
        raise ValueError("recovery_requires_new_root")
    old = store.get("question_recovery", payload["recovery_id"], optional=True)
    if not old and any(f["question_id"] == forecast["question_id"] for f in store.all("forecast")):
        raise ValueError("recovery_requires_unregistered_question")
    q = store.get("question_quarantine", payload["quarantine_id"])
    link = dict(recovery_id=payload["recovery_id"], quarantine_id=q["quarantine_id"],
                old_question_id=q["question_id"], new_question_id=forecast["question_id"],
                forecast_id=forecast["forecast_id"], review_reason=payload["review_reason"],
                contract_hash=digest(question_contract(store, forecast)),
                registered_at=old["registered_at"] if old else clock.utc())
    validate_recovery(store, link, forecast)
    from .theses import register_forecast
    # CLI ingest already owns a transaction. Direct API is atomic too.
    with nullcontext() if store.db.in_transaction else store.transaction():
        store.put("question_recovery", link["recovery_id"], link)
        result = register_forecast(store, forecast)
    return dict(recovery=link, forecast=result)


def recovery_report(store, as_of=None):
    rows = list(quarantines(store, as_of).values())
    links = [r for r in store.all("question_recovery") if not as_of or instant(r["registered_at"]) <= instant(as_of)]
    for link in links:
        validate_recovery(store, link, store.get("forecast", link["forecast_id"]))
    return dict(quarantined_questions=len(rows), quarantined_forecasts=sum(len(q["forecast_hashes"]) for q in rows),
                quarantines=rows, recovery_links=links,
                history_status="ISSUES_RETAINED" if rows else "NO_QUARANTINES")
