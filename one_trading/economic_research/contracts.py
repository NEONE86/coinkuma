"""Immutable question contracts and admissible outcome evidence.

This checks recorded evidence, not an analyst's unrecorded external knowledge.
Free-text period attribution is not an independently verified chain measurement.
"""
from copy import deepcopy

from .evidence import validate_observation
from .store import decimal, digest, instant, one_of, stamp
from .semantics import strict_bool


def measurement_target(store, forecast):
    subjects = store.get("thesis_revision", forecast["thesis_revision_id"])["subject_asset_ids"]
    if "measurement_target" in forecast:
        raise ValueError("composite_measurement_target_not_supported")
    # Explicit compatibility rule, never infer from prose or a decision.
    target = forecast.get("target_asset_id") if "target_asset_id" in forecast else (subjects[0] if len(subjects) == 1 else None)
    if not isinstance(target, str) or not target or target not in subjects:
        raise ValueError("unambiguous_target_asset_id_required")
    return target


def question_contract(store, forecast):
    rule = deepcopy(forecast["resolution_rule"])
    for key in ("window_start", "window_end"):
        rule[key] = stamp(rule[key])
    for key in ("allowed_source_urls", "allowed_source_path_prefixes"):
        rule[key] = sorted(set(rule.get(key, [])))
    rule.setdefault("observation_mode", "WINDOW_FINAL")
    for key in ("accept_source_claim", "ignore_query_for_source_match"):
        rule[key] = strict_bool(rule, key)
    strict_bool(forecast, "outcome_already_observed")
    return dict(target_asset_id=measurement_target(store, forecast),
                target_type=forecast["target_type"], deadline=stamp(forecast["deadline"]),
                resolution_rule=rule)


def contract_hash(store, forecast):
    return digest(question_contract(store, forecast))


def outcome_value(rule, value):
    value = decimal(value)
    if rule["operator"] == "NUMERIC":
        return str(value)
    threshold = decimal(rule["threshold"])
    return int({"GE": value >= threshold, "GT": value > threshold,
                "LE": value <= threshold, "LT": value < threshold,
                "EQ": value == threshold}[rule["operator"]])


def measured_outcome(store, forecast, observation, as_of, early=False):
    contract = question_contract(store, forecast)
    rule = contract["resolution_rule"]
    if (observation["asset_id"] != contract["target_asset_id"]
            or observation["unit"] != rule["unit"] or observation["metric"] != rule["measurement"]):
        raise ValueError("resolution_measurement_identity_or_unit")
    provenance = validate_observation(store, observation, as_of)
    if observation.get("value_type") != "DECIMAL":
        raise ValueError("numeric_validated_outcome_required")
    if "ASSUMPTION" in provenance["leaf_epistemic_types"] or observation["value"] is None:
        raise ValueError("assumption_or_missing_is_not_outcome")
    if "SOURCE_CLAIM" in provenance["leaf_epistemic_types"] and not rule.get("accept_source_claim", False):
        raise ValueError("source_claim_is_not_executed_outcome")
    for sid in observation["evidence_ids"]:
        url = store.get("source_snapshot", sid)["source_url"]
        checked = url.split("?", 1)[0] if rule.get("ignore_query_for_source_match") else url
        if (checked not in rule["allowed_source_urls"]
                and not any(p.endswith("/") and url.startswith(p) for p in rule.get("allowed_source_path_prefixes", []))):
            raise ValueError("resolution_source_not_preregistered")
    if observation["period_start"] is None or observation["period_end"] is None:
        raise ValueError("resolution_effective_window_not_proven")
    start, end = instant(observation["period_start"]), instant(observation["period_end"])
    lower, upper = instant(rule["window_start"]), instant(rule["window_end"])
    mode = rule.get("observation_mode", "WINDOW_FINAL")
    result = outcome_value(rule, observation["value"])
    exact = start == lower and end == upper
    # Only explicitly monotonic questions can close on a partial positive result.
    # END_STATE and TERMINAL_PRICE may reverse before the endpoint.
    decisive = (mode in {"EVER_IN_WINDOW", "MONOTONIC_CUMULATIVE"}
                and rule["operator"] in {"GE", "GT"} and result == 1
                and lower <= start <= end <= upper and end <= instant(as_of)
                and (mode == "EVER_IN_WINDOW" or start == lower))
    if not exact and not decisive:
        raise ValueError("resolution_effective_window_not_proven")
    if early and not (decisive or (exact and upper <= instant(as_of))):
        raise ValueError("outcome_not_yet_determined")
    return dict(outcome=result, measured_value=str(decimal(observation["value"])),
                outcome_epistemic_type=observation["epistemic_type"],
                outcome_leaf_epistemic_types=provenance["leaf_epistemic_types"],
                source_available_at=provenance["source_available_at"],
                resolution_observed_at=observation["available_at"])


def known_outcome_evidence(store, forecast, as_of):
    """All revisions and recorded observations count, not just the new packet."""
    siblings = {f["forecast_id"] for f in store.all("forecast") if f["question_id"] == forecast["question_id"]}
    known = [r["resolution_id"] for r in store.all("resolution")
             if r["forecast_id"] in siblings and r["status"] == "RESOLVED"
             and instant(r["resolution_at"]) <= instant(as_of)]
    for o in store.all("observation"):
        if instant(o["available_at"]) > instant(as_of):
            continue
        try:
            measured_outcome(store, forecast, o, as_of, early=True)
        except ValueError:
            continue
        known.append(o["observation_id"])
    return known


def forecast_timing(forecast):
    cutoff, registered, deadline = map(instant, (forecast["knowledge_cutoff"], forecast["registered_at"], forecast["deadline"]))
    return dict(remaining_horizon_seconds=(deadline - cutoff).total_seconds(),
                actual_lead_seconds=(deadline - registered).total_seconds(),
                elapsed_measurement_seconds=max(0, (cutoff - instant(forecast["resolution_rule"]["window_start"])).total_seconds()),
                measurement_window_start=forecast["resolution_rule"]["window_start"],
                measurement_window_end=forecast["resolution_rule"]["window_end"])
