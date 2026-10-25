"""Outcome-only scoring, evidence-linked attribution and frozen policy pairs."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, localcontext

from .evidence import evidence_links, source_bytes
from .store import (check_revision, decimal, digest, identifier, instant, nonempty,
                    one_of, required, utc)
from .theses import validate_packet

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
    old = store.get("policy_change_review", data["review_id"], optional=True)
    return store.put("policy_change_review", data["review_id"], dict(data, registered_at=old["registered_at"] if old else utc()))


def latest_resolution(store, forecast_id, as_of):
    rows = [r for r in store.all("resolution") if r["forecast_id"] == forecast_id
            and instant(r["resolution_at"]) <= instant(as_of)]
    return rows[-1] if rows else None


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
    check_revision(store, "resolution", data, "resolution_id", "supersedes_resolution_id", ("forecast_id",))
    if data["evidence_ids"]:
        evidence_links(store, data["evidence_ids"], now)
    data.update(outcome=None, measured_value=None, resolution_at=now, resolution_observed_at=None)
    if data["status"] == "RESOLVED":
        evidence_links(store, data["evidence_ids"], now)
        if len(data["observation_ids"]) != 1:
            raise ValueError("one_resolved_measurement_required")
        observation = store.get("observation", data["observation_ids"][0])
        thesis = store.get("thesis_revision", forecast["thesis_revision_id"])
        rule = forecast["resolution_rule"]
        if (observation["asset_id"] not in thesis["subject_asset_ids"]
                or observation["unit"] != rule["unit"] or observation["metric"] != rule["measurement"]):
            raise ValueError("resolution_measurement_identity_or_unit")
        if observation["epistemic_type"] == "ASSUMPTION" or observation["value"] is None:
            raise ValueError("assumption_or_missing_is_not_outcome")
        if observation["epistemic_type"] == "SOURCE_CLAIM" and not rule.get("accept_source_claim", False):
            raise ValueError("source_claim_is_not_executed_outcome")
        if not set(observation["evidence_ids"]).issubset(data["evidence_ids"]):
            raise ValueError("resolution_evidence_link_missing")
        for sid in observation["evidence_ids"]:
            snapshot = store.get("source_snapshot", sid)
            checked_url = snapshot["source_url"].split("?", 1)[0] if rule.get("ignore_query_for_source_match") else snapshot["source_url"]
            exact = checked_url in rule["allowed_source_urls"]
            prefixes = rule.get("allowed_source_path_prefixes", [])
            prefix_match = any(prefix.endswith("/") and snapshot["source_url"].startswith(prefix) for prefix in prefixes)
            if not exact and not prefix_match:
                raise ValueError("resolution_source_not_preregistered")
        if instant(observation["available_at"]) > instant(now) or instant(observation["available_at"]) <= instant(forecast["registered_at"]):
            raise ValueError("resolution_observation_time")
        # Exact measurement window; late evidence is allowed, late economic periods are not.
        if (observation["period_start"] is None or observation["period_end"] is None
                or instant(observation["period_start"]) != instant(rule["window_start"])
                or instant(observation["period_end"]) != instant(rule["window_end"])):
            raise ValueError("resolution_effective_window_not_proven")
        value = decimal(observation["value"])
        op = rule["operator"]
        if op == "NUMERIC":
            data["outcome"] = str(value)
        else:
            threshold = decimal(rule["threshold"])
            comparisons = {"GE": value >= threshold, "GT": value > threshold, "LE": value <= threshold,
                           "LT": value < threshold, "EQ": value == threshold}
            data["outcome"] = int(comparisons[op])
        data.update(measured_value=str(value), resolution_observed_at=observation["available_at"],
                    outcome_epistemic_type=observation["epistemic_type"])
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
            status = resolution["status"] if resolution else ("DATA_MISSING" if due else "PENDING")
            thesis = store.get("thesis_revision", f["thesis_revision_id"])
            packet = store.get("evidence_packet", thesis["packet_id"])
            clusters.update(store.get("economic_event_revision", rid)["independent_cluster_id"] for rid in packet["event_revision_ids"])
            if status == "RESOLVED" and due:
                counts["resolved_forecasts"] += 1
                horizon = round((instant(f["deadline"]) - instant(f["knowledge_cutoff"])).total_seconds() / 86400, 3)
                key = (f["target_type"], horizon, f["probability_or_distribution"]["type"])
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
        for (target, horizon, kind), rows in sorted(groups.items()):
            baselines = [r[1] for r in rows if r[1] is not None]
            bins = defaultdict(list)
            for _, _, _, dist, outcome in rows:
                if kind == "BINARY":
                    bucket = min(9, int(decimal(dist["p"]) * 10))
                    bins[bucket].append(outcome)
            scores.append(dict(target_type=target, horizon_days=horizon, score_name="BRIER" if kind == "BINARY" else "MAE",
                               score_denominator=len(rows), mean_loss=str(sum(r[0] for r in rows) / len(rows)),
                               baseline_denominator=len(baselines), baseline_loss=str(sum(baselines) / len(baselines)) if baselines else None,
                               mean_resolution_delay_days=sum(r[2] for r in rows) / len(rows),
                               calibration_bins=[dict(lower=str(Decimal(b) / 10), upper=str(Decimal(b + 1) / 10), n=len(v),
                                                      observed_frequency=str(sum(v) / len(v))) for b, v in sorted(bins.items())]))
    counts["independent_event_clusters"] = len(clusters)
    return dict(as_of=as_of, mode=mode, revision_policy=revision_policy, counts=dict(counts), scores=scores,
                resolved_only_training_labels=labels, coverage_denominator="SELECTED_FORECASTS_ALL_STATUSES",
                resolved_fraction=str(Decimal(counts["resolved_forecasts"]) / len(chosen)) if chosen else None,
                score_bias="Scores condition on resolved outcomes; missingness may bias them. Repeated questions and tokens are not independent trials.",
                profitability_validated=False)


def policy_pair_results(store, decisions, as_of=None):
    """Compare proposed entry versus abstention using resolved terminal prices.

    This endpoint deliberately does not simulate a position path or account PnL.
    Decision-specific entry prices and costs were fixed before the outcome.
    """
    as_of = as_of or utc()
    rows = []
    for decision in decisions:
        prices = [store.get("forecast", fid) for fid in decision["forecast_ids"]
                  if store.get("forecast", fid)["target_type"] == "PRICE"
                  and store.get("forecast", fid)["probability_or_distribution"]["type"] == "NUMERIC"
                  and store.get("forecast", fid)["deadline"] == decision["horizon"]]
        if len(prices) != 1:
            return dict(status="ENDPOINT_UNSUPPORTED", reason="A single numeric terminal-price forecast at the decision horizon is required.")
        forecast = prices[0]
        resolution = latest_resolution(store, forecast["forecast_id"], as_of)
        if not resolution or resolution["status"] != "RESOLVED":
            return dict(status="PENDING", score_denominator=0, expected_question="Fixed terminal markout versus abstention and missed winners")
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
                interpretation="One-unit proposed-entry versus cash diagnostic; no fills, account return, causal proof or capital approval.")


def compare_policies(store, change_id, decision_ids):
    change = store.get("policy_change", change_id)
    if change["status"] != "FROZEN":
        raise ValueError("comparison_requires_frozen_change")
    if len(decision_ids) != 2 or len(set(decision_ids)) != 2:
        raise ValueError("two_independent_decision_logs_required")
    decisions = [store.get("valuation_and_decision", did) for did in decision_ids]
    policies = {change["parent_policy_id"], change["challenger_policy_id"]}
    if {d["policy_version"] for d in decisions} != policies:
        raise ValueError("comparison_policy_mismatch")
    if len({d["shared_input_manifest"] for d in decisions}) != 1 or len({d["target_asset_id"] for d in decisions}) != 1:
        raise ValueError("comparison_must_share_inputs")
    for decision in decisions:
        thesis = store.get("thesis_revision", decision["thesis_revision_id"])
        packet = validate_packet(store, store.get("evidence_packet", thesis["packet_id"]))
        if instant(packet["as_of"]) < instant(change["prospective_start"]):
            raise ValueError("comparison_input_predates_preregistration")
    results = policy_pair_results(store, decisions)
    key = identifier("comparison", [change_id, sorted(decision_ids), results])
    data = dict(comparison_id=key, change_id=change_id, policy_ids=sorted(policies),
                shared_input_manifest=decisions[0]["shared_input_manifest"],
                independent_decision_logs=decision_ids, primary_endpoint=change["primary_endpoint"],
                outcome_coverage=score(store)["counts"], results=results)
    return store.put("policy_comparison", key, data)


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
            forecasts.append(dict(f, status=resolution["status"] if resolution else (
                "PENDING" if instant(f["deadline"]) > instant(as_of) else "DATA_MISSING")))
        from .valuation import value_scenarios
        price_record = store.get("price_snapshot", decision["price_snapshot_id"]) if decision["price_snapshot_id"] else None
        diagnostic = value_scenarios(price_record["value"] if price_record else decision["assumed_entry_price"],
                                    decision["scenario_tree"], decision["cost_assumptions"], decision["horizon"],
                                    price_record["quote_currency"] if price_record else decision["quote_currency"], decision["required_return"])
        cards.append(dict(decision=decision, thesis=thesis, forecasts=forecasts, packet=packet,
                          current_sensitivity_diagnostics=diagnostic,
                          price_snapshot=store.get("price_snapshot", decision["price_snapshot_id"]) if decision["price_snapshot_id"] else None,
                          observations=[store.get("observation", oid) for oid in packet["observation_ids"]],
                          sources=[store.get("source_snapshot", sid) for sid in packet["source_snapshot_ids"]]))
    receipts = [r for r in store.all("retrieval_receipt") if instant(r["received_at"]) <= instant(as_of)]
    attempts = [r for r in store.all("attempt") if instant(r["recorded_at"]) <= instant(as_of)]
    return dict(as_of=as_of, cards=cards, scoring=scoring,
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
    VersionedEvidence(store, offline=True)  # compare/recover durable receipts too
    count = store.verify()
    for source in store.all("source_snapshot"):
        source_bytes(store, source["snapshot_id"])
    for packet in store.all("evidence_packet"):
        validate_packet(store, packet)
    return dict(status="PASS", records=count, sources=len(store.all("source_snapshot")),
                packets=len(store.all("evidence_packet")), scope="Storage and packet integrity, not truth or investment edge")
