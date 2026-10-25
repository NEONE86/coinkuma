"""Outcome-only scoring, evidence-linked attribution and frozen policy pairs."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, localcontext

from .evidence import evidence_links, source_bytes
from .store import (check_revision, decimal, digest, identifier, instant, nonempty,
                    one_of, required, utc)
from .theses import validate_packet
from .contracts import (contract_hash, question_contract, measured_outcome,
                        known_outcome_evidence, forecast_timing)

FAILURES = {"IMPLEMENTATION_WRONG", "ECONOMIC_MECHANISM_WRONG", "VALUE_CAPTURE_WRONG",
            "SUPPLY_WRONG", "EXPECTATIONS_ALREADY_PRICED", "CATALYST_OR_TIMING_WRONG",
            "ENTRY_PRICE_WRONG", "EXIT_OR_HOLDING_WRONG", "EXECUTION_COST_WRONG",
            "MARKET_REGIME_DOMINANT", "DATA_OR_IDENTITY_ERROR", "UNRESOLVED"}
POLICY_FIELDS = {"policy_id", "mechanism_id", "causal_model_version", "estimation_version",
                 "decision_rule", "status", "description"}


def register_policy(store, payload):
    if set(payload) != POLICY_FIELDS:
        raise ValueError("policy_schema_no_risk_or_execution_authority_fields")
    data = dict(payload)
    nonempty(data, *POLICY_FIELDS)
    one_of(data["mechanism_id"], {"VALUE_CAPTURE_CHANGE", "NET_SUPPLY_CHANGE", "BOTH"})
    one_of(data["status"], {"IMPLEMENTATION_VALID", "RESEARCH_PRIORITY", "PROSPECTIVE_PAPER", "CAPITAL_REVIEW"})
    if not isinstance(data["decision_rule"], str):
        raise ValueError("policy_rule_must_be_descriptive_no_executable_code")
    old = store.get("policy", data["policy_id"], optional=True)
    data.update(registered_at=old["registered_at"] if old else utc(), frozen_spec_hash=digest(payload),
                execution_authority="NONE", capital_authorized=False)
    return store.put("policy", data["policy_id"], data)


def register_change(store, payload):
    allowed = {"change_id", "parent_policy_id", "challenger_policy_id", "mechanism_id", "evidence_ids",
               "identified_error", "proposed_change", "expected_gain", "expected_cost", "applicability",
               "primary_endpoint", "prospective_start", "review_rule", "rollback_rule", "status"}
    if set(payload) != allowed:
        raise ValueError("policy_change_schema_no_authority_changes")
    data = dict(payload)
    nonempty(data, *allowed)
    one_of(data["mechanism_id"], {"VALUE_CAPTURE_CHANGE", "NET_SUPPLY_CHANGE"})
    one_of(data["status"], {"PROPOSED", "FROZEN", "REJECTED"})
    if not isinstance(data["proposed_change"], list) or len(data["proposed_change"]) != 1:
        raise ValueError("one_major_change_required")
    evidence_links(store, data["evidence_ids"])
    store.get("policy", data["parent_policy_id"])
    store.get("policy", data["challenger_policy_id"])
    if data["parent_policy_id"] == data["challenger_policy_id"]:
        raise ValueError("challenger_must_differ")
    old = store.get("policy_change", data["change_id"], optional=True)
    if not old and instant(data["prospective_start"]) < instant(utc()):
        raise ValueError("comparison_start_must_be_future")
    terminal = {r["change_id"] for r in store.all("policy_change_review")}
    active = [c for c in store.all("policy_change") if c["status"] in {"PROPOSED", "FROZEN"}
              and c["change_id"] not in terminal and c["change_id"] != data["change_id"]]
    if data["status"] != "REJECTED" and any(c["mechanism_id"] == data["mechanism_id"] for c in active):
        raise ValueError("one_active_challenger_per_mechanism")
    data.update(registered_at=old["registered_at"] if old else utc(), frozen_spec_hash=digest(payload))
    return store.put("policy_change", data["change_id"], data)


def review_change(store, data):
    nonempty(data, "review_id", "change_id", "status", "reason", "evidence_ids")
    one_of(data["status"], {"REJECTED", "COMPLETED", "ROLLED_BACK"})
    store.get("policy_change", data["change_id"])
    evidence_links(store, data["evidence_ids"])
    if data["status"] == "COMPLETED":
        cohorts = [c for c in cohort_coverage(store, utc()) if c["change_id"] == data["change_id"]]
        if not cohorts or any(c["resolved_pairs"] != c["registered_pairs"] for c in cohorts):
            raise ValueError("complete_frozen_cohort_required")
    old = store.get("policy_change_review", data["review_id"], optional=True)
    return store.put("policy_change_review", data["review_id"], dict(data, registered_at=old["registered_at"] if old else utc()))


def latest_resolution(store, forecast_id, as_of):
    forecast = store.get("forecast", forecast_id)
    siblings = {f["forecast_id"] for f in store.all("forecast")
                if f["question_id"] == forecast["question_id"]
                and instant(f["registered_at"]) <= instant(as_of)
                and contract_hash(store, f) == contract_hash(store, forecast)}
    rows = [r for r in store.all("resolution") if r["forecast_id"] in siblings
            and instant(r["resolution_at"]) <= instant(as_of)]
    return rows[-1] if rows else None


def effective_status(forecast, resolution, as_of):
    status = resolution["status"] if resolution else "PENDING"
    return "DATA_MISSING" if status == "PENDING" and instant(forecast["deadline"]) <= instant(as_of) else status


def validate_resolved_record(store, forecast, resolution):
    """Legacy resolved records must pass the same raw-provenance gate as new ones.

    Fail closed until an explicit superseding resolution is registered; never
    silently change an immutable outcome or train on a grandfathered bad label.
    """
    if len(resolution["observation_ids"]) != 1:
        raise ValueError("one_resolved_measurement_required")
    observation = store.get("observation", resolution["observation_ids"][0])
    result = measured_outcome(store, forecast, observation, resolution["resolution_at"])
    if (decimal(result["outcome"]) != decimal(resolution["outcome"])
            or not set(observation["evidence_ids"]).issubset(resolution["evidence_ids"])
            or instant(result["source_available_at"]) <= instant(forecast["registered_at"])
            or instant(resolution["resolution_at"]) < instant(forecast["deadline"])):
        raise ValueError("inadmissible_stored_resolution")
    return result


def resolve_forecast(store, payload):
    data = dict(payload)
    nonempty(data, "resolution_id", "forecast_id", "status", "explanation")
    required(data, "evidence_ids", "observation_ids", "supersedes_resolution_id")
    one_of(data["status"], {"PENDING", "RESOLVED", "DATA_MISSING", "AMBIGUOUS"})
    forecast = store.get("forecast", data["forecast_id"])
    old = store.get("resolution", data["resolution_id"], optional=True)
    now = old["resolution_at"] if old else utc()
    if instant(forecast["deadline"]) > instant(now) and data["status"] != "PENDING":
        raise ValueError("unmatured_resolution")
    if data["status"] == "PENDING" and instant(forecast["deadline"]) <= instant(now):
        raise ValueError("due_outcome_needs_missing_or_ambiguous_status")
    previous = latest_resolution(store, forecast["forecast_id"], now)
    if not old and data["supersedes_resolution_id"] != (previous["resolution_id"] if previous else None):
        raise ValueError("question_resolution_parent_required")
    if old and data["supersedes_resolution_id"] != old["supersedes_resolution_id"]:
        raise ValueError("resolution_parent_immutable")
    if data["evidence_ids"]:
        evidence_links(store, data["evidence_ids"], now)
    data.update(outcome=None, measured_value=None, resolution_at=now, resolution_observed_at=None)
    if data["status"] == "RESOLVED":
        evidence_links(store, data["evidence_ids"], now)
        if len(data["observation_ids"]) != 1:
            raise ValueError("one_resolved_measurement_required")
        observation = store.get("observation", data["observation_ids"][0])
        measurement = measured_outcome(store, forecast, observation, now)
        if not set(observation["evidence_ids"]).issubset(data["evidence_ids"]):
            raise ValueError("resolution_evidence_link_missing")
        if instant(measurement["source_available_at"]) <= instant(forecast["registered_at"]):
            raise ValueError("resolution_observation_time")
        data.update(measurement, endpoint_contract_hash=contract_hash(store, forecast))
    elif data["observation_ids"]:
        for oid in data["observation_ids"]:
            store.get("observation", oid)
    return store.put("resolution", data["resolution_id"], data)


def mark_due_missing(store, as_of=None):
    as_of = as_of or utc()
    if instant(as_of) > instant(utc()):
        raise ValueError("future_resolution_clock")
    result = []
    for forecast in store.all("forecast"):
        last = latest_resolution(store, forecast["forecast_id"], as_of)
        if instant(forecast["deadline"]) <= instant(as_of) and (last is None or last["status"] == "PENDING"):
            result.append(resolve_forecast(store, dict(
                resolution_id=identifier("missing", forecast["forecast_id"]), forecast_id=forecast["forecast_id"],
                status="DATA_MISSING", evidence_ids=[], observation_ids=[],
                supersedes_resolution_id=last["resolution_id"] if last else None,
                explanation="Deadline passed; no admissible measured outcome received. Not false or zero.")))
    return result


def register_attribution(store, payload):
    data = dict(payload)
    nonempty(data, "attribution_id", "decision_id", "possible_failure_causes", "certainty", "proposed_learning")
    required(data, "supporting_evidence_ids", "conflicting_evidence_ids")
    store.get("valuation_and_decision", data["decision_id"])
    if not set(data["possible_failure_causes"]).issubset(FAILURES):
        raise ValueError("unknown_failure_code")
    one_of(data["certainty"], {"HYPOTHESIS", "SUPPORTED", "UNRESOLVED"})
    evidence_links(store, data["supporting_evidence_ids"])
    if data["conflicting_evidence_ids"]:
        evidence_links(store, data["conflicting_evidence_ids"])
    data["supporting_and_conflicting_evidence"] = {"support": data["supporting_evidence_ids"], "conflict": data["conflicting_evidence_ids"]}
    old = store.get("attribution", data["attribution_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    return store.put("attribution", data["attribution_id"], data)


def score(store, as_of=None, revision_policy="INITIAL", mode="PROSPECTIVE"):
    as_of = as_of or utc()
    if instant(as_of) > instant(utc()):
        raise ValueError("future_report_clock")
    one_of(revision_policy, {"INITIAL", "LATEST_AS_OF"})
    all_forecasts = [f for f in store.all("forecast") if instant(f["registered_at"]) <= instant(as_of) and f["mode"] == mode]
    chosen = {}
    for f in all_forecasts:
        if revision_policy == "LATEST_AS_OF" or f["question_id"] not in chosen:
            chosen[f["question_id"]] = f
    counts = Counter(registered_forecasts=len(all_forecasts), selected_forecasts=len(chosen), due_forecasts=0,
                     resolved_forecasts=0, unresolved_forecasts=0, pending=0, data_missing=0, ambiguous=0)
    groups, clusters, labels = defaultdict(list), set(), []
    with localcontext() as context:
        context.prec = 80
        for f in chosen.values():
            due = instant(f["deadline"]) <= instant(as_of)
            counts["due_forecasts"] += int(due)
            resolution = latest_resolution(store, f["forecast_id"], as_of)
            status = effective_status(f, resolution, as_of)
            thesis = store.get("thesis_revision", f["thesis_revision_id"])
            packet = store.get("evidence_packet", thesis["packet_id"])
            clusters.update(store.get("economic_event_revision", rid)["independent_cluster_id"] for rid in packet["event_revision_ids"])
            if status == "RESOLVED" and due:
                validate_resolved_record(store, f, resolution)
                counts["resolved_forecasts"] += 1
                timing = forecast_timing(f)
                horizon = timing["remaining_horizon_seconds"]
                lead = timing["actual_lead_seconds"]
                dist = f["probability_or_distribution"]
                # Numeric errors only pool identical measurement definitions and
                # units. Exact horizon/lead avoids mixing refreshed forecasts.
                definition = digest(dict(subjects=question_contract(store, f)["subject_asset_ids"],
                                         rule=f["resolution_rule"])) if dist["type"] == "NUMERIC" else "BINARY"
                key = (f["target_type"], horizon, lead, dist["type"], f["resolution_rule"]["unit"], definition)
                dist = f["probability_or_distribution"]
                outcome = decimal(resolution["outcome"])
                baseline = None
                if dist["type"] == "BINARY":
                    loss = (decimal(dist["p"]) - outcome) ** 2
                    if f["base_rate"] is not None:
                        baseline = (decimal(f["base_rate"]) - outcome) ** 2
                else:
                    mean = sum(decimal(p["p"]) * decimal(p["value"]) for p in dist["points"])
                    loss = abs(mean - outcome)
                delay = (instant(resolution["resolution_at"]) - instant(f["deadline"])).total_seconds() / 86400
                groups[key].append((loss, baseline, delay, dist, outcome))
                labels.append(dict(forecast_id=f["forecast_id"], resolution_id=resolution["resolution_id"],
                                   outcome=resolution["outcome"], independent_clusters=f["independent_cluster_ids"]))
            else:
                counts["unresolved_forecasts"] += 1
                counts[status.lower()] += 1
        scores = []
        for (target, horizon, lead, kind, unit, definition), rows in sorted(groups.items()):
            baselines = [r[1] for r in rows if r[1] is not None]
            bins = defaultdict(list)
            for _, _, _, dist, outcome in rows:
                if kind == "BINARY":
                    bucket = min(9, int(decimal(dist["p"]) * 10))
                    bins[bucket].append(outcome)
            scores.append(dict(target_type=target, horizon_days=horizon / 86400,
                               remaining_horizon_seconds=horizon, actual_lead_seconds=lead,
                               unit=unit if kind == "NUMERIC" else "DIMENSIONLESS", measurement_definition_hash=definition,
                               score_name="BRIER" if kind == "BINARY" else "MAE",
                               score_denominator=len(rows), mean_loss=str(sum(r[0] for r in rows) / len(rows)),
                               baseline_denominator=len(baselines), baseline_loss=str(sum(baselines) / len(baselines)) if baselines else None,
                               mean_resolution_delay_days=sum(r[2] for r in rows) / len(rows),
                               calibration_bins=[dict(lower=str(Decimal(b) / 10), upper=str(Decimal(b + 1) / 10), n=len(v),
                                                      observed_frequency=str(sum(v) / len(v))) for b, v in sorted(bins.items())]))
    counts["independent_event_clusters"] = len(clusters)
    return dict(as_of=as_of, mode=mode, revision_policy=revision_policy, counts=dict(counts), scores=scores,
                selection_rule="First registered revision per question" if revision_policy == "INITIAL" else "Latest revision registered by as_of per immutable question",
                selected_forecasts=[dict(forecast_id=f["forecast_id"], question_id=f["question_id"], **forecast_timing(f)) for f in chosen.values()],
                resolved_only_training_labels=labels, coverage_denominator="SELECTED_FORECASTS_ALL_STATUSES",
                resolved_fraction=str(Decimal(counts["resolved_forecasts"]) / len(chosen)) if chosen else None,
                score_bias="Scores condition on resolved outcomes; missingness may bias them. Repeated questions and tokens are not independent trials.",
                profitability_validated=False)


def comparison_endpoint(store, decisions):
    endpoints = []
    for decision in decisions:
        forecasts = [store.get("forecast", fid) for fid in decision["forecast_ids"]]
        prices = [f for f in forecasts if f["target_type"] == "PRICE"
                  and instant(f["deadline"]) == instant(decision["horizon"])]
        if len(prices) != 1:
            raise ValueError("one_common_price_endpoint_required")
        endpoints.append(prices[0])
    if len({contract_hash(store, f) for f in endpoints}) != 1:
        raise ValueError("comparison_endpoint_mismatch")
    # Same question gives every probability revision the same outcome history.
    if len({f["question_id"] for f in endpoints}) != 1:
        raise ValueError("comparison_common_question_required")
    return endpoints[0]


def policy_pair_results(store, decisions, as_of=None):
    """Compare proposed entry versus abstention using resolved terminal prices.

    This endpoint deliberately does not simulate a position path or account PnL.
    Decision-specific entry prices and costs were fixed before the outcome.
    """
    as_of = as_of or utc()
    endpoint = comparison_endpoint(store, decisions)
    common_resolution = latest_resolution(store, endpoint["forecast_id"], as_of)
    rows = []
    for decision in decisions:
        prices = [store.get("forecast", fid) for fid in decision["forecast_ids"]
                  if store.get("forecast", fid)["target_type"] == "PRICE"
                  and store.get("forecast", fid)["probability_or_distribution"]["type"] == "NUMERIC"
                  and store.get("forecast", fid)["deadline"] == decision["horizon"]]
        if len(prices) != 1:
            return dict(status="ENDPOINT_UNSUPPORTED", reason="A single numeric terminal-price forecast at the decision horizon is required.")
        forecast = prices[0]
        resolution = common_resolution
        if not resolution or resolution["status"] != "RESOLVED":
            return dict(status="PENDING", score_denominator=0, expected_question="Fixed terminal markout versus abstention and missed winners")
        validate_resolved_record(store, forecast, resolution)
        if forecast["resolution_rule"].get("price_metric") != "TERMINAL_PRICE":
            return dict(status="ENDPOINT_UNSUPPORTED", reason="Numeric return or excess return is not a terminal price.")
        if not decision["price_snapshot_id"] or decision["price_status"] != "OBSERVED":
            return dict(status="CONDITIONAL_ONLY", score_denominator=0)
        price = store.get("price_snapshot", decision["price_snapshot_id"])
        if forecast["resolution_rule"]["unit"] != price["quote_currency"]:
            raise ValueError("comparison_outcome_currency_mismatch")
        if any(decimal(s["cash_distribution"]) != 0 for s in decision["scenario_tree"]["scenarios"] if "terminal_price" in s):
            return dict(status="ENDPOINT_UNSUPPORTED", reason="Actual distributions require a separate resolved measurement.")
        terminal = decimal(resolution["outcome"])
        costs = decision["cost_assumptions"]
        markout = terminal * (1 - decimal(costs["sell_fraction"])) / (decimal(price["value"]) * (1 + decimal(costs["buy_fraction"]))) - 1
        entry_selected = decision["investment_action"] == "PAPER_ENTER"
        rows.append(dict(policy_id=decision["policy_version"], decision_id=decision["decision_id"],
                         action=decision["investment_action"], terminal_markout=str(markout),
                         proposal_payoff=str(markout if entry_selected else Decimal(0)),
                         missed_positive_markout=str(max(Decimal(0), markout) if not entry_selected else Decimal(0)),
                         resolution_id=resolution["resolution_id"]))
    return dict(status="RESOLVED", independent_event_pairs=1, policies=rows,
                common_question_id=endpoint["question_id"], endpoint_contract_hash=contract_hash(store, endpoint),
                common_resolution_id=common_resolution["resolution_id"],
                interpretation="One-unit proposed-entry versus cash diagnostic; no fills, account return, causal proof or capital approval.")


def compare_policies(store, change_id, decision_ids):
    change = store.get("policy_change", change_id)
    if change["status"] != "FROZEN":
        raise ValueError("comparison_requires_frozen_change")
    if len(decision_ids) != 2 or len(set(decision_ids)) != 2:
        raise ValueError("two_independent_decision_logs_required")
    decision_ids = sorted(decision_ids)
    decisions = [store.get("valuation_and_decision", did) for did in decision_ids]
    policies = {change["parent_policy_id"], change["challenger_policy_id"]}
    if {d["policy_version"] for d in decisions} != policies:
        raise ValueError("comparison_policy_mismatch")
    if len({d["shared_input_manifest"] for d in decisions}) != 1 or len({d["target_asset_id"] for d in decisions}) != 1:
        raise ValueError("comparison_must_share_inputs")
    common_inputs = []
    for decision in decisions:
        thesis = store.get("thesis_revision", decision["thesis_revision_id"])
        packet = validate_packet(store, store.get("evidence_packet", thesis["packet_id"]))
        if instant(packet["as_of"]) < instant(change["prospective_start"]):
            raise ValueError("comparison_input_predates_preregistration")
        common_inputs.append(digest(dict(packet=packet["manifest_hash"], price=decision["price_snapshot_id"],
                                        horizon=decision["horizon"], costs=decision["cost_assumptions"],
                                        benchmark=decision["benchmark_snapshot_id"], asset=decision["target_asset_id"])))
    if len(set(common_inputs)) != 1:
        raise ValueError("comparison_must_share_inputs")
    endpoint = comparison_endpoint(store, decisions)
    pair_id = identifier("pair", [change_id, decision_ids])
    pair = store.get("policy_pair", pair_id, optional=True)
    if pair is None:
        if any(p["change_id"] == change_id and p["question_id"] == endpoint["question_id"] for p in store.all("policy_pair")):
            raise ValueError("question_already_enrolled_in_policy_pair")
        if (instant(endpoint["deadline"]) <= instant(utc()) or known_outcome_evidence(store, endpoint, utc())):
            raise ValueError("pair_must_be_registered_before_outcome")
        if any(c["change_id"] == change_id for c in store.all("policy_cohort")):
            raise ValueError("policy_cohort_already_frozen")
        pair = store.put("policy_pair", pair_id, dict(pair_id=pair_id, change_id=change_id,
            decision_ids=decision_ids, common_input_hash=common_inputs[0],
            endpoint_contract_hash=contract_hash(store, endpoint), question_id=endpoint["question_id"], registered_at=utc()))
    results = policy_pair_results(store, decisions)
    key = identifier("comparison", [change_id, sorted(decision_ids), results])
    data = dict(comparison_id=key, change_id=change_id, policy_ids=sorted(policies),
                shared_input_manifest=decisions[0]["shared_input_manifest"],
                independent_decision_logs=decision_ids, primary_endpoint=change["primary_endpoint"],
                pair_id=pair_id, endpoint_contract_hash=pair["endpoint_contract_hash"], results=results,
                scope="PREREGISTERED_PAIR_DIAGNOSTIC_NOT_POLICY_PROMOTION")
    return store.put("policy_comparison", key, data)


def freeze_policy_cohort(store, payload):
    """Freeze the full event set before any outcome; no favorable-result subset."""
    nonempty(payload, "cohort_id", "change_id", "pair_ids")
    pairs = [store.get("policy_pair", pid) for pid in payload["pair_ids"]]
    all_pairs = [p for p in store.all("policy_pair") if p["change_id"] == payload["change_id"]]
    if len(pairs) != len({p["pair_id"] for p in pairs}) or {p["pair_id"] for p in pairs} != {p["pair_id"] for p in all_pairs}:
        raise ValueError("cohort_must_include_all_enrolled_pairs")
    old = store.get("policy_cohort", payload["cohort_id"], optional=True)
    if not old:
        if any(c["change_id"] == payload["change_id"] for c in store.all("policy_cohort")):
            raise ValueError("one_frozen_cohort_per_change")
        for pair in pairs:
            endpoint = comparison_endpoint(store, [store.get("valuation_and_decision", did) for did in pair["decision_ids"]])
            if instant(endpoint["deadline"]) <= instant(utc()) or known_outcome_evidence(store, endpoint, utc()):
                raise ValueError("cohort_must_precede_outcomes")
    data = dict(payload, pair_ids=sorted(payload["pair_ids"]), registered_at=old["registered_at"] if old else utc())
    return store.put("policy_cohort", data["cohort_id"], data)


def cohort_coverage(store, as_of):
    result = []
    for cohort in store.all("policy_cohort"):
        if instant(cohort["registered_at"]) > instant(as_of):
            continue
        pairs = []
        for pid in cohort["pair_ids"]:
            pair = store.get("policy_pair", pid)
            values = policy_pair_results(store, [store.get("valuation_and_decision", did) for did in pair["decision_ids"]], as_of)
            pairs.append(dict(pair_id=pid, results=values))
        result.append(dict(cohort_id=cohort["cohort_id"], change_id=cohort["change_id"], as_of=as_of,
                           registered_pairs=len(pairs), resolved_pairs=sum(p["results"]["status"] == "RESOLVED" for p in pairs),
                           pairs=pairs, capital_authorized=False))
    return result


def report(store, as_of=None):
    as_of = as_of or utc()
    scoring = score(store, as_of)
    latest = {}
    for decision in store.all("valuation_and_decision"):
        if instant(decision["decision_at"]) <= instant(as_of):
            latest[decision["decision_series_id"]] = decision
    cards = []
    for decision in latest.values():
        thesis = store.get("thesis_revision", decision["thesis_revision_id"])
        packet = validate_packet(store, store.get("evidence_packet", thesis["packet_id"]))
        forecasts = []
        for fid in decision["forecast_ids"]:
            f = store.get("forecast", fid)
            resolution = latest_resolution(store, fid, as_of)
            forecasts.append(dict(f, status=effective_status(f, resolution, as_of),
                                  question_contract=question_contract(store, f), timing=forecast_timing(f)))
        from .valuation import value_scenarios
        price_record = store.get("price_snapshot", decision["price_snapshot_id"]) if decision["price_snapshot_id"] else None
        diagnostic = value_scenarios(price_record["value"] if price_record else decision["assumed_entry_price"],
                                    decision["scenario_tree"], decision["cost_assumptions"], decision["horizon"],
                                    price_record["quote_currency"] if price_record else decision["quote_currency"], decision["required_return"])
        source_ids = set(packet["source_snapshot_ids"])
        if price_record:
            source_ids.update(price_record["evidence_ids"])
        if decision["benchmark_snapshot_id"]:
            source_ids.update(store.get("price_snapshot", decision["benchmark_snapshot_id"])["evidence_ids"])
        cards.append(dict(decision=decision, thesis=thesis, forecasts=forecasts, packet=packet,
                          current_sensitivity_diagnostics=diagnostic,
                          price_snapshot=store.get("price_snapshot", decision["price_snapshot_id"]) if decision["price_snapshot_id"] else None,
                          observations=[store.get("observation", oid) for oid in packet["observation_ids"]],
                          sources=[store.get("source_snapshot", sid) for sid in sorted(source_ids)]))
    receipts = [r for r in store.all("retrieval_receipt") if instant(r["received_at"]) <= instant(as_of)]
    attempts = [r for r in store.all("attempt") if instant(r["recorded_at"]) <= instant(as_of)]
    return dict(as_of=as_of, cards=cards, scoring=scoring, policy_cohort_coverage=cohort_coverage(store, as_of),
                candidate_comparisons=[r for r in store.all("candidate_comparison") if instant(r["registered_at"]) <= instant(as_of)],
                source_coverage=dict(attempts=len(receipts), successes=sum(not r.get("error") for r in receipts),
                                     failures=[r for r in receipts if r.get("error")],
                                     pipeline_failures=[r for r in attempts if r["operation"] == "collect" and r["status"] == "REJECTED"]),
                rejected_import_attempts=[r for r in attempts if r["operation"] in {"register", "ingest"} and r["status"] == "REJECTED"],
                reasoner_backend="manual_import", model_call_status="NOT_RUN", paper_fills=0,
                limitations=["No approved paper broker or position ledger connected.",
                             "Source hashes establish byte integrity, not truth or profitability.",
                             "Probabilities and terminal values are subjective; no calibrated empirical base rate yet."])


def audit(store):
    from .evidence import VersionedEvidence
    from .integrity import inspect_semantics
    VersionedEvidence(store, offline=True)  # compare/recover durable receipts too
    count = store.verify()
    for source in store.all("source_snapshot"):
        source_bytes(store, source["snapshot_id"])
    for packet in store.all("evidence_packet"):
        validate_packet(store, packet)
    semantics = inspect_semantics(store)
    return dict(status=semantics["status"], records=count, sources=len(store.all("source_snapshot")),
                packets=len(store.all("evidence_packet")), semantics=semantics,
                scope="Storage, packet and listed semantic checks; not truth or investment edge")
