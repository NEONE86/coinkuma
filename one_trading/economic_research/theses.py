"""Frozen evidence packets, manual-import reasoning, prospective forecasts."""
from __future__ import annotations

from datetime import timedelta

from .evidence import evidence_links, source_bytes
from .store import (check_revision, decimal, digest, identifier, instant, nonempty,
                    one_of, required, stamp, utc)
from .valuation import probability, value_scenarios
from .contracts import contract_hash, question_contract, known_outcome_evidence, forecast_timing
from .semantics import chain_key, predictor_id, LEGACY_PREDICTOR


def check_forecast_revision(store, data):
    chain = chain_key(data)
    rows = store.all("forecast")
    parent_id = data["supersedes_forecast_id"]
    if parent_id:
        parent = store.get("forecast", parent_id)
        if parent_id == data["forecast_id"] or chain_key(parent) != chain:
            raise ValueError("revision_identity_mismatch")
        if any(f["supersedes_forecast_id"] == parent_id and f["forecast_id"] != data["forecast_id"] for f in rows):
            raise ValueError("revision_branch_requires_new_identity")
    elif any(chain_key(f) == chain and f["forecast_id"] != data["forecast_id"] for f in rows):
        raise ValueError("revision_parent_required")


def validate_decision_targets(store, data, forecasts):
    for forecast in forecasts:
        if forecast["target_type"] == "PRICE" and question_contract(store, forecast)["target_asset_id"] != data["target_asset_id"]:
            raise ValueError("decision_price_forecast_target_mismatch")
        if predictor_id(forecast) not in {LEGACY_PREDICTOR, data["policy_version"]}:
            raise ValueError("decision_forecast_policy_mismatch")

MODES = {"CASE_STUDY_BACKFILL", "HISTORICAL_POINT_IN_TIME", "PROSPECTIVE"}


def _references(store, packet):
    refs = []
    for kind, key in (("asset_identity", "subject_asset_ids"),
                      ("economic_event_revision", "event_revision_ids"),
                      ("economic_flow", "economic_flow_ids"),
                      ("observation", "observation_ids"),
                      ("source_snapshot", "source_snapshot_ids"),
                      ("price_snapshot", "price_snapshot_ids")):
        for rid in packet[key]:
            refs.append([kind, rid, digest(store.get(kind, rid))])
    return refs


def packet_manifest(store, packet):
    header = {k: v for k, v in packet.items() if k not in {"packet_id", "manifest_hash"}}
    return digest(dict(packet=header, input_hashes=_references(store, packet)))


def validate_packet(store, packet):
    as_of = instant(packet["as_of"])
    if as_of > instant(utc()):
        raise ValueError("future_packet_cutoff")
    subjects = set(packet["subject_asset_ids"])
    for kind, rid, _ in _references(store, packet):
        record = store.get(kind, rid)
        available = record.get("available_at", record.get("registered_at"))
        if available is None or instant(available) > as_of:
            raise ValueError("future_packet_input")
        if "asset_id" in record and record["asset_id"] not in subjects and kind != "asset_identity":
            raise ValueError("packet_asset_mismatch")
    evidence_links(store, packet["source_snapshot_ids"], packet["as_of"])
    if packet["manifest_hash"] != packet_manifest(store, packet):
        raise ValueError("packet_manifest_tampered")
    return packet


def make_packet(store, asset_ids, as_of=None):
    as_of = stamp(as_of or utc())
    if len(set(asset_ids)) != len(asset_ids) or not asset_ids:
        raise ValueError("packet_subjects")
    assets = [store.get("asset_identity", aid) for aid in asset_ids]
    selected = {}
    excluded = {rid for review in store.all("collection_link_review")
                if instant(review["available_at"]) <= instant(as_of)
                for rid in review["excluded_event_revision_ids"]}
    for event in store.all("economic_event_revision"):
        if (event["asset_id"] in asset_ids and instant(event["available_at"]) <= instant(as_of)
                and event["revision_id"] not in excluded):
            selected[event["event_id"]] = event
    events = list(selected.values())
    if not events:
        raise ValueError("no_available_event")
    oids = sorted({oid for event in events for oid in event["observation_ids"]})
    observations = [store.get("observation", oid) for oid in oids]
    prices = {}
    for price in store.all("price_snapshot"):
        if price["asset_id"] in asset_ids and instant(price["available_at"]) <= instant(as_of):
            prices[price["asset_id"]] = price
    sids = {sid for o in observations for sid in o["evidence_ids"]}
    for asset in assets:
        sids.update(asset["identity_evidence_ids"])
        sids.update(asset["migration_evidence_ids"])
        for mapping in asset["exchange_symbol_mapping"].values():
            sids.update(mapping["evidence_ids"])
    for price in prices.values():
        sids.update(price["evidence_ids"])
    for event in events:
        for fid in event["economic_flow_ids"]:
            for leg in store.get("economic_flow", fid)["legs"]:
                sids.update(leg["evidence_ids"])
    packet = dict(as_of=as_of, subject_asset_ids=sorted(asset_ids),
                  event_ids=sorted(selected), event_revision_ids=sorted(e["revision_id"] for e in events),
                  economic_flow_ids=sorted({fid for e in events for fid in e["economic_flow_ids"]}),
                  observation_ids=oids, source_snapshot_ids=sorted(sids),
                  price_snapshot_ids=sorted(p["price_snapshot_id"] for p in prices.values()))
    packet["manifest_hash"] = packet_manifest(store, packet)
    packet["packet_id"] = identifier("packet", packet)
    validate_packet(store, packet)
    return store.put("evidence_packet", packet["packet_id"], packet)


def register_thesis(store, data):
    data = dict(data)
    nonempty(data, "thesis_id", "revision_id", "subject_asset_ids", "event_ids", "packet_id",
             "causal_model_version", "causal_chain", "authority_and_incentives", "primary_explanation",
             "competing_explanations", "market_expectation_gap", "next_observations", "falsifiers",
             "next_high_value_question", "model_id", "prompt_hash", "request_response_hashes")
    required(data, "supersedes_revision_id", "evidence_manifest_hash", "mode", "reasoner_backend", "model_call_status")
    one_of(data["mode"], MODES)
    if data["reasoner_backend"] != "manual_import" or data["model_call_status"] != "NOT_RUN":
        raise ValueError("only_manual_import_no_api_call_implemented")
    packet = validate_packet(store, store.get("evidence_packet", data["packet_id"]))
    if sorted(data["subject_asset_ids"]) != packet["subject_asset_ids"] or sorted(data["event_ids"]) != packet["event_ids"]:
        raise ValueError("thesis_packet_link_mismatch")
    if data["evidence_manifest_hash"] != packet["manifest_hash"]:
        raise ValueError("thesis_manifest_mismatch")
    for explanation in data["competing_explanations"]:
        nonempty(explanation, "explanation", "distinguishing_observation")
    check_revision(store, "thesis_revision", data, "revision_id", "supersedes_revision_id", ("thesis_id", "subject_asset_ids"))
    old = store.get("thesis_revision", data["revision_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    if instant(packet["as_of"]) > instant(data["registered_at"]):
        raise ValueError("packet_after_registration")
    return store.put("thesis_revision", data["revision_id"], data)


def register_forecast(store, data):
    data = dict(data)
    nonempty(data, "forecast_id", "question_id", "thesis_revision_id", "target_type", "target_definition",
             "deadline", "probability_or_distribution", "estimate_basis", "uncertainty_assumptions",
             "resolution_rule", "estimate_rationale")
    required(data, "reference_class_id", "supersedes_forecast_id", "knowledge_cutoff", "base_rate")
    one_of(data["target_type"], {"IMPLEMENTATION", "ECONOMICS", "PRICE"})
    one_of(data["estimate_basis"], {"EMPIRICAL_REFERENCE", "MODEL", "JUDGMENT", "MIXED"})
    thesis = store.get("thesis_revision", data["thesis_revision_id"])
    packet = validate_packet(store, store.get("evidence_packet", thesis["packet_id"]))
    if instant(data["knowledge_cutoff"]) != instant(packet["as_of"]):
        raise ValueError("knowledge_cutoff_mismatch")
    if data.get("probability_source") in {"TOKEN_LIKELIHOOD", "MODEL_CONFIDENCE"}:
        raise ValueError("token_likelihood_is_not_event_probability")
    dist = data["probability_or_distribution"]
    one_of(dist.get("type"), {"BINARY", "NUMERIC"})
    if dist["type"] == "BINARY":
        probability(dist["p"])
    else:
        nonempty(dist, "points", "unit")
        if sum(probability(point["p"]) for point in dist["points"]) != 1:
            raise ValueError("probability_sum")
        for point in dist["points"]:
            decimal(point["value"])
    if data["base_rate"] is not None:
        probability(data["base_rate"])
    rule = data["resolution_rule"]
    nonempty(rule, "measurement", "unit", "window_start", "window_end", "allowed_source_urls",
             "missing_data_rule", "effective_time_rule")
    required(rule, "operator", "threshold")
    from .evidence import validate_url
    for url in rule["allowed_source_urls"]:
        validate_url(url)
    for prefix in rule.get("allowed_source_path_prefixes", []):
        validate_url(prefix)
        if not prefix.endswith("/") or "?" in prefix:
            raise ValueError("invalid_resolution_source_prefix")
    one_of(rule["operator"], {"GE", "GT", "LE", "LT", "EQ", "NUMERIC"})
    if (dist["type"] == "NUMERIC") != (rule["operator"] == "NUMERIC"):
        raise ValueError("forecast_rule_type_mismatch")
    if rule["operator"] != "NUMERIC":
        decimal(rule["threshold"])
    if instant(rule["window_end"]) != instant(data["deadline"]) or instant(rule["window_start"]) > instant(rule["window_end"]):
        raise ValueError("resolution_window_mismatch")
    if data["target_type"] == "PRICE":
        nonempty(rule, "price_spec", "benchmark_spec")
    if dist["type"] == "NUMERIC" and dist["unit"] != rule["unit"]:
        raise ValueError("forecast_distribution_unit_mismatch")
    mode = rule.get("observation_mode", "WINDOW_FINAL")
    one_of(mode, {"WINDOW_FINAL", "END_STATE", "TERMINAL_PRICE", "EVER_IN_WINDOW", "MONOTONIC_CUMULATIVE"})
    if mode in {"EVER_IN_WINDOW", "MONOTONIC_CUMULATIVE"} and (dist["type"] != "BINARY" or rule["operator"] not in {"GE", "GT"}):
        raise ValueError("monotonic_question_requires_positive_threshold_rule")
    if data["target_type"] == "PRICE" and mode in {"EVER_IN_WINDOW", "MONOTONIC_CUMULATIVE"}:
        raise ValueError("terminal_price_cannot_close_early")
    contract = question_contract(store, data)
    check_forecast_revision(store, data)
    from .recovery import check_new_question
    check_new_question(store, data, contract)
    old = store.get("forecast", data["forecast_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    data["resolution_due_at"] = data["deadline"]
    data["mode"] = thesis["mode"]
    if predictor_id(data) != LEGACY_PREDICTOR:
        policy = store.get("policy", data["policy_id"])
        if instant(policy["registered_at"]) > instant(data["knowledge_cutoff"]):
            raise ValueError("policy_must_precede_forecast_cutoff")
    parent = store.get("forecast", data["supersedes_forecast_id"]) if data["supersedes_forecast_id"] else None
    if parent and (instant(data["knowledge_cutoff"]) <= instant(parent["knowledge_cutoff"])
                   or instant(data["registered_at"]) <= instant(parent["registered_at"])):
        raise ValueError("revision_clock_must_advance")
    if instant(data["knowledge_cutoff"]) > instant(data["registered_at"]):
        raise ValueError("cutoff_after_registration")
    if data["mode"] == "PROSPECTIVE":
        if instant(data["deadline"]) <= instant(data["registered_at"]) or data.get("outcome_already_observed"):
            raise ValueError("prospective_outcome_already_known_or_due")
        if parent and parent["mode"] != "PROSPECTIVE":
            raise ValueError("cannot_promote_retrospective_question")
        if not parent and instant(rule["window_start"]) < instant(packet["as_of"]):
            raise ValueError("prospective_window_before_cutoff")
        if not old and known_outcome_evidence(store, data, data["registered_at"]):
            raise ValueError("prospective_outcome_already_known_or_due")
    data["independent_cluster_ids"] = sorted({store.get("economic_event_revision", rid)["independent_cluster_id"]
                                              for rid in packet["event_revision_ids"]})
    # Preserve legacy serialized records on idempotent replay; new rows carry the
    # explicit contract, which is always independently recomputed when compared.
    if not old:
        data.update(question_contract_hash=digest(contract), question_contract=contract,
                    timing=forecast_timing(data))
    else:
        for key in ("question_contract_hash", "question_contract", "timing"):
            if key in old:
                data[key] = old[key]  # Keep legacy bytes; validation recomputes semantics.
    return store.put("forecast", data["forecast_id"], data)


def register_decision(store, data):
    data = dict(data)
    nonempty(data, "decision_id", "decision_series_id", "target_asset_id", "thesis_revision_id", "forecast_ids",
             "scenario_tree", "cost_assumptions", "horizon", "research_choice", "investment_action",
             "reason", "change_conditions", "estimation_version", "policy_version")
    required(data, "price_snapshot_id", "price_status", "benchmark_snapshot_id", "action_execution_status",
             "portfolio_snapshot_id", "paper_fill_ids", "supersedes_decision_id", "required_return")
    thesis = store.get("thesis_revision", data["thesis_revision_id"])
    packet = validate_packet(store, store.get("evidence_packet", thesis["packet_id"]))
    if data["target_asset_id"] not in thesis["subject_asset_ids"]:
        raise ValueError("decision_asset_mismatch")
    one_of(data["research_choice"], {"PRIORITIZE", "MONITOR", "ARCHIVE"})
    one_of(data["investment_action"], {"NO_TRADE", "WATCH", "PAPER_ENTER", "PAPER_HOLD", "PAPER_ADD", "PAPER_REDUCE", "PAPER_EXIT"})
    # No paper executor/configuration has been approved or linked in this version.
    # Fail closed on applied trades and invented holdings; proposals create no fills.
    if data["action_execution_status"] != "PROPOSED" or data["paper_fill_ids"] or data["portfolio_snapshot_id"]:
        raise ValueError("paper_executor_not_connected")
    if data["investment_action"] in {"PAPER_HOLD", "PAPER_ADD", "PAPER_REDUCE", "PAPER_EXIT"}:
        raise ValueError("actual_paper_portfolio_required_executor_not_connected")
    if len(set(data["forecast_ids"])) != len(data["forecast_ids"]):
        raise ValueError("duplicate_forecast_link")
    forecasts = [store.get("forecast", fid) for fid in data["forecast_ids"]]
    validate_decision_targets(store, data, forecasts)
    if any(f["thesis_revision_id"] != data["thesis_revision_id"] for f in forecasts):
        raise ValueError("decision_forecast_mismatch")
    if not any(f["target_type"] == "PRICE" and instant(f["deadline"]) == instant(data["horizon"]) for f in forecasts):
        raise ValueError("same_horizon_price_forecast_required")
    check_revision(store, "valuation_and_decision", data, "decision_id", "supersedes_decision_id",
                   ("decision_series_id", "target_asset_id", "policy_version"))
    old = store.get("valuation_and_decision", data["decision_id"], optional=True)
    data["decision_at"] = old["decision_at"] if old else utc()
    if instant(data["decision_at"]) < instant(thesis["registered_at"]) or instant(data["horizon"]) <= instant(data["decision_at"]):
        raise ValueError("invalid_decision_time")
    one_of(data["price_status"], {"OBSERVED", "MISSING", "STALE"})
    if data["price_snapshot_id"]:
        price = store.get("price_snapshot", data["price_snapshot_id"])
        if price["asset_id"] != data["target_asset_id"] or instant(price["available_at"]) > instant(data["decision_at"]):
            raise ValueError("decision_price_identity_or_time")
        age = instant(data["decision_at"]) - instant(price["source_time"])
        if data["price_status"] == "OBSERVED" and (age > timedelta(hours=1) or age.total_seconds() < 0):
            raise ValueError("price_not_fresh")
        entry, currency = price["value"], price["quote_currency"]
    else:
        if data["price_status"] != "MISSING" or data["investment_action"] == "PAPER_ENTER":
            raise ValueError("missing_price_no_current_entry")
        nonempty(data, "assumed_entry_price", "quote_currency", "price_missing_reason")
        entry, currency = data["assumed_entry_price"], data["quote_currency"]
    if data["price_status"] != "OBSERVED" and data["investment_action"] == "PAPER_ENTER":
        raise ValueError("stale_price_no_current_entry")
    if data["benchmark_snapshot_id"]:
        benchmark = store.get("price_snapshot", data["benchmark_snapshot_id"])
        if instant(benchmark["available_at"]) > instant(data["decision_at"]):
            raise ValueError("future_benchmark")
    result = value_scenarios(entry, data["scenario_tree"], data["cost_assumptions"], data["horizon"], currency, data["required_return"])
    data["valuation"] = result
    data["sensitivity_results"] = result["price_sensitivity"]
    policy = store.get("policy", data["policy_version"])
    if instant(policy["registered_at"]) > instant(data["decision_at"]):
        raise ValueError("future_policy")
    data["shared_input_manifest"] = digest(dict(packet=packet["manifest_hash"], price=data["price_snapshot_id"],
                                                horizon=data["horizon"], costs=data["cost_assumptions"]))
    return store.put("valuation_and_decision", data["decision_id"], data)


def register_bundle(store, payload):
    """Transactional import; timestamps supplied by an analyst are not trusted."""
    key = identifier("import", payload)
    existing = store.get("registration", key, optional=True)
    if existing:
        # Check inputs again on replay, including raw tampering.
        revision = payload["thesis"]["revision_id"]
        if (payload["decision"]["thesis_revision_id"] != revision
                or any(f["thesis_revision_id"] != revision for f in payload["forecasts"])
                or store.get("valuation_and_decision", existing["decision_id"])["thesis_revision_id"] != revision
                or any(store.get("forecast", fid)["thesis_revision_id"] != revision for fid in existing["forecast_ids"])):
            raise ValueError("bundle_thesis_links")
        validate_packet(store, store.get("evidence_packet", payload["thesis"]["packet_id"]))
        forecasts = [store.get("forecast", fid) for fid in existing["forecast_ids"]]
        for forecast in forecasts:
            question_contract(store, forecast)
        validate_decision_targets(store, store.get("valuation_and_decision", existing["decision_id"]), forecasts)
        return existing
    try:
        with store.transaction():
            revision = payload["thesis"]["revision_id"]
            if (payload["decision"]["thesis_revision_id"] != revision
                    or any(f["thesis_revision_id"] != revision for f in payload["forecasts"])):
                raise ValueError("bundle_thesis_links")
            thesis = register_thesis(store, payload["thesis"])
            forecasts = [register_forecast(store, f) for f in payload["forecasts"]]
            if {f["target_type"] for f in forecasts} != {"IMPLEMENTATION", "ECONOMICS", "PRICE"}:
                raise ValueError("three_forecast_targets_required")
            decision = register_decision(store, payload["decision"])
            if set(decision["forecast_ids"]) != {f["forecast_id"] for f in forecasts}:
                raise ValueError("bundle_forecast_links")
            result = dict(registration_id=key, thesis_revision_id=thesis["revision_id"],
                          forecast_ids=[f["forecast_id"] for f in forecasts], decision_id=decision["decision_id"],
                          registered_at=utc(), payload_hash=digest(payload))
            store.put("registration", key, result)
    except Exception as exc:
        store.attempt("register", payload, "REJECTED", str(exc))
        raise
    store.attempt("register", payload, "ACCEPTED", key)
    return result
