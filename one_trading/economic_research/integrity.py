"""Read-only semantic impact inspection, including legacy v4 records.

No record is repaired or re-registered here. Issues identify records requiring an
explicit correction; raw hash validity is not economic truth verification.
"""
from collections import defaultdict

from .contracts import question_contract, contract_hash, measured_outcome, known_outcome_evidence
from .evidence import validate_observation
from .store import decimal, digest, instant
from .valuation import value_scenarios


def inspect_semantics(store):
    checks = {"F%02d" % n: dict(checked_record_ids=[], issues=[]) for n in range(1, 14)}

    def check(code, key, operation):
        checks[code]["checked_record_ids"].append(key)
        try:
            operation()
        except (ValueError, KeyError, TypeError) as exc:
            checks[code]["issues"].append(dict(record_id=key, reason=str(exc)))

    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    for o in store.all("observation"):
        code = "F02" if o["epistemic_type"] == "DERIVED_VALUE" else "F01"
        check(code, o["observation_id"], lambda o=o: validate_observation(store, o))
    questions = defaultdict(list)
    for f in store.all("forecast"):
        questions[f["question_id"]].append(f)
        def timing(f=f):
            t = store.get("thesis_revision", f["thesis_revision_id"])
            p = store.get("evidence_packet", t["packet_id"])
            require(instant(p["as_of"]) == instant(f["knowledge_cutoff"]) <= instant(f["registered_at"]), "cutoff_registration_mismatch")
            if f["mode"] == "PROSPECTIVE":
                require(instant(f["registered_at"]) < instant(f["deadline"]), "not_prospective")
            if f["supersedes_forecast_id"]:
                parent = store.get("forecast", f["supersedes_forecast_id"])
                require(instant(parent["knowledge_cutoff"]) < instant(f["knowledge_cutoff"])
                        and instant(parent["registered_at"]) < instant(f["registered_at"]), "revision_clock_did_not_advance")
        check("F03", f["forecast_id"], timing)
        dist = f["probability_or_distribution"]
        if dist["type"] == "NUMERIC":
            check("F05", f["forecast_id"], lambda f=f, dist=dist: require(dist["unit"] == f["resolution_rule"]["unit"], "distribution_unit_mismatch"))
        check("F11", f["forecast_id"], lambda: None)  # Effective status derived by report, not edited.
    for qid, revisions in questions.items():
        def identity(revisions=revisions):
            require(len({contract_hash(store, f) for f in revisions}) == 1, "question_contract_changed")
            require(sum(not f["supersedes_forecast_id"] for f in revisions) == 1, "question_root_count")
            previous = None
            for f in revisions:
                require(f["supersedes_forecast_id"] == previous, "question_lineage")
                previous = f["forecast_id"]
                if f["mode"] == "PROSPECTIVE":
                    require(not known_outcome_evidence(store, f, f["registered_at"]), "outcome_known_at_registration")
        check("F04", qid, identity)
    for r in store.all("resolution"):
        if r["status"] == "RESOLVED":
            def resolution(r=r):
                f = store.get("forecast", r["forecast_id"])
                require(len(r["observation_ids"]) == 1, "one_measurement_required")
                o = store.get("observation", r["observation_ids"][0])
                result = measured_outcome(store, f, o, r["resolution_at"])
                require(decimal(result["outcome"]) == decimal(r["outcome"]), "stored_outcome_mismatch")
                require(instant(result["source_available_at"]) > instant(f["registered_at"]), "outcome_source_predates_forecast")
            check("F02", r["resolution_id"], resolution)
            check("F06", r["resolution_id"], resolution)
    for r in store.all("registration"):
        def links(r=r):
            ids = [store.get("forecast", fid)["thesis_revision_id"] for fid in r["forecast_ids"]]
            d = store.get("valuation_and_decision", r["decision_id"])
            require(set(ids + [d["thesis_revision_id"]]) == {r["thesis_revision_id"]}, "bundle_thesis_links")
            require(set(d["forecast_ids"]) == set(r["forecast_ids"]), "bundle_forecast_links")
        check("F09", r["registration_id"], links)
    for d in store.all("valuation_and_decision"):
        def value(d=d):
            p = store.get("price_snapshot", d["price_snapshot_id"]) if d["price_snapshot_id"] else None
            v = value_scenarios(p["value"] if p else d["assumed_entry_price"], d["scenario_tree"], d["cost_assumptions"],
                                d["horizon"], p["quote_currency"] if p else d["quote_currency"], d["required_return"])
            for key in ("expected_return", "break_even_entry", "entry_for_required_return", "scenarios"):
                require(v[key] == d["valuation"][key], "stored_valuation_mismatch:" + key)
        check("F08", d["decision_id"], value)
    for c in store.all("policy_comparison"):
        from .review import comparison_endpoint
        check("F07", c["comparison_id"], lambda c=c: comparison_endpoint(store, [store.get("valuation_and_decision", did) for did in c["independent_decision_logs"]]))
        check("F10", c["comparison_id"], lambda c=c: require("outcome_coverage" not in c, "mutable_coverage_in_immutable_result"))
    seen_refs, seen_legs = set(), set()
    for f in store.all("economic_flow"):
        def flow(f=f):
            ids = [leg["leg_id"] for leg in f["legs"]]
            refs = [leg["external_reference"] for leg in f["legs"]]
            scoped_ids = {(f["asset_id"], key) for key in ids}
            require(len(set(ids)) == len(ids) and len(set(refs)) == len(refs)
                    and not (set(refs) & seen_refs) and not (scoped_ids & seen_legs), "duplicate_flow_leg")
            seen_refs.update(refs)
            seen_legs.update(scoped_ids)
        check("F12", f["economic_flow_id"], flow)
    prior_facts = []
    for e in store.all("economic_event_revision"):
        def event(e=e):
            facts = {fid for oid in e["observation_ids"]
                     for fid in validate_observation(store, store.get("observation", oid))["fact_ids"]}
            for old, old_facts in prior_facts:
                if old["event_id"] != e["event_id"]:
                    require(not (facts == old_facts and old["mechanism_id"] == e["mechanism_id"]), "duplicate_economic_event")
                    require(not (facts & old_facts) or old["independent_cluster_id"] == e["independent_cluster_id"], "shared_fact_cluster")
            prior_facts.append((e, facts))
        check("F13", e["revision_id"], event)
    for item in checks.values():
        item["status"] = "ISSUES" if item["issues"] else "PASS" if item["checked_record_ids"] else "NOT_EXERCISED_IN_LEDGER"
    return dict(status="ISSUES" if any(v["issues"] for v in checks.values()) else "PASS", checks=checks,
                scope="Stored semantic contracts and numerical evidence; not truth, external knowledge, chain completeness, or investment edge")
