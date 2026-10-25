"""F01-F13 regression assertions; all market inputs and clocks are synthetic."""
import copy
from datetime import timedelta
from decimal import Decimal
import unittest

import test_economic_research_v4 as fixture
from test_economic_research_v4 import SOURCE, CONTRACT, policy
from one_trading.economic_research import evidence, events, review, theses
from one_trading.economic_research.contracts import question_contract
from one_trading.economic_research.render import markdown
from one_trading.economic_research.store import digest


class FixTests(unittest.TestCase):
    def setUp(self):
        self.t = fixture.V4Tests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def measurement(self, value="1", kind="DIRECT_RECORD", key="out", fid="IMPLEMENTATION"):
        t = self.t
        f = t.store.get("forecast", fid)
        r = f["resolution_rule"]
        sid = t.fetch(SOURCE, dict(contract=CONTRACT, value=value, measurement=r["measurement"], key=key))
        return dict(observation_id=key, asset_id=t.aid, metric=r["measurement"], unit=r["unit"],
                    value=value, value_type="DECIMAL", period_start=r["window_start"], period_end=r["window_end"],
                    epistemic_type=kind, evidence_ids=[sid], locator=dict(snapshot_id=sid, json_pointer="/value"),
                    derivation_version=None)

    def resolve(self, o, fid="IMPLEMENTATION", key="res", parent=None):
        return review.resolve_forecast(self.t.store, dict(resolution_id=key, forecast_id=fid,
            status="RESOLVED", explanation="Synthetic measured result", evidence_ids=o["evidence_ids"],
            observation_ids=[o["observation_id"]], supersedes_resolution_id=parent))

    def updated_bundle(self):
        t = self.t
        sid = t.fetch(SOURCE, dict(contract=CONTRACT, value="200"))
        o = t.observation("new-budget", "reported_budget", "200", sid)
        events.register_event(t.store, dict(t.event, revision_id="event-r2", supersedes_revision_id="event-r1",
                                            observation_ids=[o["observation_id"]]))
        price = t.ticker("100")
        packet = theses.make_packet(t.store, [t.aid])
        b = copy.deepcopy(t.payload)
        b["thesis"].update(revision_id="thesis-r2", supersedes_revision_id="thesis-r1", packet_id=packet["packet_id"],
                           evidence_manifest_hash=packet["manifest_hash"])
        for f in b["forecasts"]:
            f.update(forecast_id=f["forecast_id"] + "-r2", supersedes_forecast_id=f["forecast_id"],
                     thesis_revision_id="thesis-r2", knowledge_cutoff=packet["as_of"])
            f["probability_or_distribution"]["p"] = "0.9"
        b["decision"].update(decision_id="decision-r2", supersedes_decision_id="decision-r1", thesis_revision_id="thesis-r2",
                              price_snapshot_id=price["price_snapshot_id"], forecast_ids=[f["forecast_id"] for f in b["forecasts"]])
        return b

    def numeric(self, f, value="100", unit="USDT"):
        f["probability_or_distribution"] = dict(type="NUMERIC", unit=unit, points=[dict(p="1", value=value)])
        f["resolution_rule"].update(operator="NUMERIC", threshold=None, unit=unit)
        if f["target_type"] == "PRICE":
            f["resolution_rule"]["price_metric"] = "TERMINAL_PRICE"
        return f

    def pair(self, mismatch=False):
        t = self.t
        review.register_policy(t.store, policy("challenger"))
        change = dict(change_id="change", parent_policy_id="incumbent", challenger_policy_id="challenger",
            mechanism_id="VALUE_CAPTURE_CHANGE", evidence_ids=[t.sid], identified_error="Synthetic",
            proposed_change=["Different selection"], expected_gain="Fewer losses", expected_cost="Miss winners",
            applicability="M1", primary_endpoint="Common terminal markout", prospective_start=(t.now + timedelta(seconds=1)).isoformat(),
            review_rule="All frozen pairs", rollback_rule="No improvement", status="FROZEN")
        review.register_change(t.store, change)
        t.advance(seconds=2)
        t.packet = theses.make_packet(t.store, [t.aid])
        t.payload = t.bundle()
        self.numeric(t.payload["forecasts"][-1])
        t.registered()
        ids = ["IMPLEMENTATION", "ECONOMICS", "PRICE"]
        if mismatch:
            alt = copy.deepcopy(t.payload["forecasts"][-1])
            alt.update(forecast_id="ALT", question_id="ALT")
            alt["resolution_rule"]["measurement"] = "alternate_price"
            theses.register_forecast(t.store, alt)
            ids[-1] = "ALT"
        theses.register_decision(t.store, dict(t.payload["decision"], decision_id="challenger-decision", decision_series_id="challenger-series",
                                               policy_version="challenger", forecast_ids=ids))
        return ["decision-r1", "challenger-decision"]

    def test_F01_numeric_type_required_and_raw_zero_brier(self):
        t = self.t
        t.registered(); t.advance(days=181)
        original = self.measurement("0")
        for changes in ({"value_type": None}, {"value_type": "UNKNOWN"}, {"value": True},
                        {"value": 1.0}, {"value_type": "TEXT", "value": 1}, {"value": "1"}):
            bad = dict(original, **changes)
            if changes.get("value_type", "present") is None:
                del bad["value_type"]
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evidence.observe(t.store, bad)
        good = evidence.observe(t.store, original)
        self.assertEqual(self.resolve(good)["outcome"], 0)
        s = review.score(t.store)
        self.assertEqual(Decimal(s["scores"][0]["mean_loss"]), Decimal("0.64"))

    def test_F01_text_is_not_numeric_outcome(self):
        t = self.t
        t.registered(); t.advance(days=181)
        o = evidence.observe(t.store, dict(self.measurement(), value_type="TEXT"))
        with self.assertRaisesRegex(ValueError, "numeric_validated"):
            self.resolve(o)

    def test_F02_recursive_assumption_and_source_claim(self):
        t = self.t
        t.payload["forecasts"][0]["resolution_rule"]["accept_source_claim"] = False
        t.registered(); t.advance(days=181)
        for kind in ("ASSUMPTION", "SOURCE_CLAIM", "DIRECT_RECORD"):
            o = evidence.observe(t.store, self.measurement(kind=kind, key=kind))
            for depth in (1, 2):
                o = evidence.observe(t.store, dict(o, observation_id=kind + str(depth), epistemic_type="DERIVED_VALUE",
                    input_observation_ids=[o["observation_id"]], calculation="SUM", derivation_version="sum-v1"))
            if kind != "DIRECT_RECORD":
                with self.assertRaisesRegex(ValueError, "assumption_or_missing|source_claim_is_not"):
                    self.resolve(o, key="res-" + kind)
            else:
                self.assertEqual(self.resolve(o)["outcome_leaf_epistemic_types"], ["DIRECT_RECORD"])

    def test_F02_explicit_claim_permission_preserves_status(self):
        t = self.t
        t.registered(); t.advance(days=181)
        o = evidence.observe(t.store, self.measurement(kind="SOURCE_CLAIM"))
        derived = evidence.observe(t.store, dict(o, observation_id="sum", epistemic_type="DERIVED_VALUE",
            input_observation_ids=[o["observation_id"]], calculation="SUM", derivation_version="sum-v1"))
        self.assertEqual(self.resolve(derived)["outcome_leaf_epistemic_types"], ["SOURCE_CLAIM"])

    def test_F02_period_future_missing_input_cycle_and_metadata(self):
        t = self.t
        a = dict(t.obs, observation_id="sum", epistemic_type="DERIVED_VALUE", input_observation_ids=[t.obs["observation_id"]],
                 calculation="SUM", derivation_version="sum-v1", input_epistemic_types=["DIRECT_RECORD"])
        for bad in (dict(a, period_end=t.time()), dict(a, input_observation_ids=["absent"]),
                    dict(a, available_at="1999-01-01T00:00:00Z"), dict(a, unit="TOKEN")):
            with self.assertRaises(ValueError):
                evidence.validate_observation(t.store, bad)
        circular = dict(a, input_observation_ids=["sum"])
        t.store.put("observation", "sum", circular)
        with self.assertRaisesRegex(ValueError, "cycle"):
            evidence.validate_observation(t.store, circular)

    def test_F02_valid_multi_input_sum_and_legacy_bad_resolution_exclusion(self):
        t = self.t
        t.registered(); t.advance(days=181)
        a = evidence.observe(t.store, self.measurement("1", key="a"))
        b = evidence.observe(t.store, self.measurement("2", key="b"))
        total = evidence.observe(t.store, dict(a, observation_id="total", value="3", epistemic_type="DERIVED_VALUE",
            input_observation_ids=["a", "b"], calculation="SUM", derivation_version="sum-v1", evidence_ids=a["evidence_ids"]+b["evidence_ids"]))
        self.assertEqual(self.resolve(total)["measured_value"], "3")
        bad = dict(t.store.get("resolution", "res"), resolution_id="legacy-bad", outcome=0, supersedes_resolution_id="res")
        t.store.put("resolution", "legacy-bad", bad)  # Simulate an imported pre-fix record.
        with self.assertRaisesRegex(ValueError, "inadmissible_stored"):
            review.score(t.store)

    def test_F04_old_scalar_cannot_be_retimestamped_with_new_citation(self):
        t = self.t
        t.registered(); t.advance(days=181)
        old = self.measurement("100")
        new_sid = old["evidence_ids"][0]
        old.update(evidence_ids=[t.sid,new_sid], locator=dict(snapshot_id=t.sid,json_pointer="/value"))
        o = evidence.observe(t.store, old)
        with self.assertRaisesRegex(ValueError, "observation_time"):
            self.resolve(o)

    def test_F03_new_evidence_revision_end_to_end_and_shared_outcome(self):
        t = self.t
        t.registered()
        initial = t.store.get("forecast", "IMPLEMENTATION")
        original_hash = digest(initial)
        t.advance(days=30)
        b = self.updated_bundle()
        theses.register_bundle(t.store, b)
        revision = t.store.get("forecast", "IMPLEMENTATION-r2")
        self.assertEqual(question_contract(t.store, initial), question_contract(t.store, revision))
        self.assertEqual(revision["timing"]["remaining_horizon_seconds"], 150 * 86400)
        self.assertEqual(digest(t.store.get("forecast", "IMPLEMENTATION")), original_hash)
        self.assertEqual(review.score(t.store)["selected_forecasts"][0]["forecast_id"], "IMPLEMENTATION")
        self.assertEqual(review.score(t.store, revision_policy="LATEST_AS_OF")["selected_forecasts"][0]["forecast_id"], "IMPLEMENTATION-r2")
        t.advance(days=151)
        self.resolve(evidence.observe(t.store, self.measurement("0")))
        self.assertEqual(Decimal(review.score(t.store)["scores"][0]["mean_loss"]), Decimal(".64"))
        self.assertEqual(Decimal(review.score(t.store, revision_policy="LATEST_AS_OF")["scores"][0]["mean_loss"]), Decimal(".81"))
        self.assertEqual(markdown(review.report(t.store)), markdown(review.report(t.store)))

    def test_F04_new_root_cannot_hide_failed_question(self):
        t = self.t
        t.registered(); t.advance(days=181)
        self.resolve(evidence.observe(t.store, self.measurement("0")))
        changed = copy.deepcopy(t.payload["forecasts"][0])
        changed.update(forecast_id="later", deadline=(t.now + timedelta(days=30)).isoformat())
        changed["resolution_rule"]["window_end"] = changed["deadline"]
        changed["resolution_rule"]["window_start"] = t.time()
        with self.assertRaisesRegex(ValueError, "parent_required"):
            theses.register_forecast(t.store, changed)
        changed["question_id"] = "new-question"
        theses.register_forecast(t.store, changed)
        scores = review.score(t.store, revision_policy="LATEST_AS_OF")
        self.assertEqual(scores["counts"]["selected_forecasts"], 4)
        self.assertEqual(scores["counts"]["resolved_forecasts"], 1)

    def test_F04_asset_is_part_of_contract(self):
        t = self.t
        t.registered(); t.advance(seconds=1)
        other = "0x" + "2" * 40
        sid = t.fetch(SOURCE, dict(contract=other, value="100"))
        asset = copy.deepcopy(t.asset)
        asset.update(asset_id="eip155:1:" + other, contract_address=other, identity_evidence_ids=[sid])
        asset["exchange_symbol_mapping"]["BINANCE"]["instrument"] = "OTHERUSDT"
        events.register_asset(t.store, asset)
        o = evidence.observe(t.store, dict(t.obs, observation_id="other", asset_id=asset["asset_id"],
            evidence_ids=[sid], locator=dict(snapshot_id=sid, json_pointer="/value")))
        events.register_event(t.store, dict(t.event, event_id="other", revision_id="other-r1", asset_id=asset["asset_id"],
            observation_ids=[o["observation_id"]], independent_cluster_id="other"))
        p = theses.make_packet(t.store, [asset["asset_id"]])
        th = dict(t.payload["thesis"], thesis_id="other", revision_id="other-thesis", subject_asset_ids=[asset["asset_id"]],
            event_ids=p["event_ids"], packet_id=p["packet_id"], evidence_manifest_hash=p["manifest_hash"])
        theses.register_thesis(t.store, th)
        f = dict(t.payload["forecasts"][0], forecast_id="other-forecast", supersedes_forecast_id="IMPLEMENTATION",
                 thesis_revision_id="other-thesis", knowledge_cutoff=p["as_of"])
        with self.assertRaisesRegex(ValueError, "contract_immutable"):
            theses.register_forecast(t.store, f)

    def test_F04_known_partial_positive_blocks_without_formal_resolution(self):
        t = self.t
        t.payload["forecasts"][0]["resolution_rule"]["observation_mode"] = "EVER_IN_WINDOW"
        t.registered(); t.advance(days=30)
        raw = self.measurement()
        raw.update(period_start=t.time(), period_end=t.time())
        o = evidence.observe(t.store, raw)
        t.advance(seconds=1)
        alias = evidence.observe(t.store, dict(o, observation_id="reparsed"))
        self.assertEqual(alias["available_at"], o["available_at"])
        with self.assertRaisesRegex(ValueError, "already_known"):
            theses.register_bundle(t.store, self.updated_bundle())
        self.assertEqual(t.store.all("resolution"), [])
        self.assertEqual(len(t.store.all("forecast")), 3)

    def test_F04_end_state_does_not_close_on_partial_positive(self):
        t = self.t
        t.payload["forecasts"][0]["resolution_rule"]["observation_mode"] = "END_STATE"
        t.registered(); t.advance(days=30)
        raw = self.measurement()
        raw.update(period_start=t.time(), period_end=t.time())
        evidence.observe(t.store, raw)
        theses.register_bundle(t.store, self.updated_bundle())
        self.assertEqual(len(t.store.all("forecast")), 6)

    def test_F04_revision_cutoff_and_registration_must_advance(self):
        t = self.t
        t.registered(); t.advance(seconds=1)
        f = dict(t.payload["forecasts"][0], forecast_id="r2", supersedes_forecast_id="IMPLEMENTATION")
        with self.assertRaisesRegex(ValueError, "clock_must_advance"):
            theses.register_forecast(t.store, f)

    def test_F05_all_numeric_targets_require_matching_units(self):
        t = self.t
        for f in t.payload["forecasts"]:
            self.numeric(f)
            f["probability_or_distribution"]["unit"] = "AAVE"
        for i in range(3):
            b = copy.deepcopy(t.payload)
            for j, f in enumerate(b["forecasts"]):
                if j != i:
                    f["probability_or_distribution"]["unit"] = "USDT"
            with self.assertRaisesRegex(ValueError, "unit_mismatch"):
                theses.register_bundle(t.store, b)
        self.assertEqual(t.store.all("forecast"), [])

    def test_F06_no_mixed_unit_mae(self):
        t = self.t
        f = self.numeric(t.payload["forecasts"][1], "10")
        t.registered()
        extra = copy.deepcopy(f)
        extra.update(forecast_id="SUPPLY", question_id="SUPPLY")
        self.numeric(extra, "1000", "TOKEN")
        extra["resolution_rule"]["measurement"] = "supply"
        theses.register_forecast(t.store, extra)
        t.advance(days=181)
        for fid in ("ECONOMICS", "SUPPLY"):
            self.resolve(evidence.observe(t.store, self.measurement("0", fid=fid, key=fid)), fid=fid, key="res-" + fid)
        scores = review.score(t.store)["scores"]
        self.assertEqual({(s["unit"], Decimal(s["mean_loss"])) for s in scores}, {("USDT", Decimal(10)), ("TOKEN", Decimal(1000))})
        self.assertTrue(all(s["score_denominator"] == 1 for s in scores))

    def test_F07_different_endpoints_rejected_before_results(self):
        ids = self.pair(mismatch=True)
        with self.assertRaisesRegex(ValueError, "endpoint_mismatch"):
            review.compare_policies(self.t.store, "change", ids)

    def test_F07_common_outcomes_and_F10_repeat_after_unrelated_forecast(self):
        t = self.t
        ids = self.pair()
        pending = review.compare_policies(t.store, "change", ids)
        with self.assertRaisesRegex(ValueError, "complete_frozen_cohort_required"):
            review.review_change(t.store, dict(review_id="premature", change_id="change", status="COMPLETED", reason="premature",
                                                evidence_ids=[t.sid]))
        with self.assertRaisesRegex(ValueError, "all_enrolled_pairs"):
            review.freeze_policy_cohort(t.store, dict(cohort_id="bad-cohort", change_id="change", pair_ids=[pending["pair_id"], pending["pair_id"]]))
        twin = dict(t.payload["decision"], decision_id="twin", decision_series_id="twin")
        theses.register_decision(t.store, twin)
        with self.assertRaisesRegex(ValueError, "already_enrolled"):
            review.compare_policies(t.store, "change", ["twin", "challenger-decision"])
        review.freeze_policy_cohort(t.store, dict(cohort_id="cohort", change_id="change", pair_ids=[pending["pair_id"]]))
        t.advance(days=181)
        parent = None
        for value, expected in (("50", "-0.5"), ("200", "1")):
            t.advance(seconds=1)
            o = evidence.observe(t.store, self.measurement(value, fid="PRICE", key="price-" + value))
            resolved = self.resolve(o, fid="PRICE", key="res-" + value, parent=parent)
            parent = resolved["resolution_id"]
            result = review.compare_policies(t.store, "change", ids)
            self.assertEqual([r["proposal_payoff"] for r in result["results"]["policies"]], [expected, expected])
        extra = copy.deepcopy(t.payload["forecasts"][0])
        extra.update(forecast_id="unrelated", question_id="unrelated", deadline=(t.now + timedelta(days=7)).isoformat())
        extra["resolution_rule"].update(window_start=t.time(), window_end=extra["deadline"])
        theses.register_forecast(t.store, extra)
        self.assertEqual(result, review.compare_policies(t.store, "change", list(reversed(ids))))
        self.assertEqual(review.report(t.store)["policy_cohort_coverage"][0]["registered_pairs"], 1)
        self.assertEqual(review.report(t.store)["policy_cohort_coverage"][0]["resolved_pairs"], 1)

    def test_F07_no_post_outcome_pair_selection(self):
        t = self.t
        ids = self.pair(); t.advance(days=181)
        with self.assertRaisesRegex(ValueError, "before_outcome"):
            review.compare_policies(t.store, "change", ids)

    def test_F03_F07_full_revision_common_outcome_policy_workflow(self):
        t = self.t
        self.pair()
        initial = digest(t.store.get("forecast", "IMPLEMENTATION"))
        t.advance(days=30)
        updated = self.updated_bundle()
        theses.register_bundle(t.store, updated)
        challenger = dict(updated["decision"], decision_id="challenger-r2", decision_series_id="challenger-series",
                          supersedes_decision_id="challenger-decision", policy_version="challenger")
        theses.register_decision(t.store, challenger)
        ids = ["decision-r2", "challenger-r2"]
        paired = review.compare_policies(t.store, "change", ids)
        review.freeze_policy_cohort(t.store, dict(cohort_id="full-cohort", change_id="change", pair_ids=[paired["pair_id"]]))
        t.advance(days=151)
        for fid, value in (("IMPLEMENTATION", "0"), ("PRICE", "50")):
            self.resolve(evidence.observe(t.store, self.measurement(value, fid=fid, key=fid)), fid=fid, key="res-" + fid)
        for mode, expected in (("INITIAL", ".64"), ("LATEST_AS_OF", ".81")):
            s = review.score(t.store, revision_policy=mode)
            loss = next(s for s in s["scores"] if s["target_type"] == "IMPLEMENTATION")
            self.assertEqual(Decimal(loss["mean_loss"]), Decimal(expected))
        result = review.compare_policies(t.store, "change", ids)
        self.assertEqual({p["proposal_payoff"] for p in result["results"]["policies"]}, {"-0.5"})
        self.assertEqual(result, review.compare_policies(t.store, "change", ids))
        self.assertEqual(initial, digest(t.store.get("forecast", "IMPLEMENTATION")))
        self.assertEqual(markdown(review.report(t.store)), markdown(review.report(t.store)))

    def test_F08_joint_probabilities_dynamic_horizon_distribution(self):
        t = self.t
        b = t.payload
        deadline = (t.now + timedelta(days=7)).isoformat()
        for f in b["forecasts"]:
            f["deadline"] = deadline
            f["resolution_rule"]["window_end"] = deadline
        b["decision"]["horizon"] = deadline
        leaves = b["decision"]["scenario_tree"]["scenarios"]
        for s in leaves:
            s.update(horizon=deadline, cash_distribution="5", distribution_kind="CASH")
        b["decision"]["scenario_tree"] = dict(kind="CONDITIONAL", scenarios=[dict(id="branch", p=".2", parent_id=None),
            dict(leaves[0], p=".8", parent_id=None), dict(leaves[1], p=".1", parent_id="branch"), dict(leaves[2], p=".9", parent_id="branch")])
        t.registered()
        text = markdown(review.report(t.store))
        self.assertIn("| success | 18.00% |", text)
        self.assertIn("100.00%", text)
        self.assertIn(deadline, text)
        self.assertIn("| 5.0000 |", text)
        self.assertNotIn("180일", text)
        self.assertNotIn("현금분배 0", text)

    def test_F09_mismatched_bundle_is_atomic(self):
        t = self.t
        b = copy.deepcopy(t.payload)
        b["thesis"]["revision_id"] = "different"
        with self.assertRaisesRegex(ValueError, "bundle_thesis_links"):
            theses.register_bundle(t.store, b)
        for kind in ("thesis_revision", "forecast", "valuation_and_decision", "registration"):
            self.assertEqual(t.store.all(kind), [])

    def test_F11_pending_history_becomes_effectively_missing(self):
        t = self.t
        t.registered()
        review.resolve_forecast(t.store, dict(resolution_id="pending", forecast_id="IMPLEMENTATION", status="PENDING",
            explanation="Not due", evidence_ids=[], observation_ids=[], supersedes_resolution_id=None))
        t.advance(days=181)
        self.assertEqual(review.score(t.store)["counts"]["data_missing"], 3)
        self.assertTrue(all(f["status"] == "DATA_MISSING" for f in review.report(t.store)["cards"][0]["forecasts"]))
        self.assertEqual(t.store.get("resolution", "pending")["status"], "PENDING")

    def test_F12_duplicate_reference_rejected_before_flow_storage(self):
        t = self.t
        flow = t.flow()
        flow["legs"].append(dict(flow["legs"][0], leg_id="alias"))
        with self.assertRaisesRegex(ValueError, "duplicate_economic_flow"):
            events.register_flow(t.store, flow)
        self.assertEqual(t.store.all("economic_flow"), [])
        good = t.flow()
        events.register_flow(t.store, good)
        second = copy.deepcopy(good)
        second["economic_flow_id"] = "second"
        for leg in second["legs"]:
            leg["external_reference"] += "-different"
        with self.assertRaisesRegex(ValueError, "duplicate_economic_flow"):
            events.register_flow(t.store, second)

    def test_F13_observation_alias_and_reimport_do_not_inflate_clusters(self):
        t = self.t
        t.advance(days=1)
        sid = t.fetch(SOURCE, dict(contract=CONTRACT, statement="Revenue may fund token purchases", value="100"))
        self.assertEqual(sid, t.sid)
        alias = evidence.observe(t.store, dict(t.obs, observation_id="alias"))
        self.assertEqual(alias["available_at"], t.obs["available_at"])
        with self.assertRaisesRegex(ValueError, "duplicate_economic_event"):
            events.register_event(t.store, dict(t.event, event_id="alias", revision_id="alias-event",
                observation_ids=[alias["observation_id"]], independent_cluster_id="fake-independent"))
        t.price = t.ticker("100")
        t.payload["decision"]["price_snapshot_id"] = t.price["price_snapshot_id"]
        t.registered()
        self.assertEqual(review.score(t.store)["counts"]["independent_event_clusters"], 1)

    def test_F13_distinct_effective_period_is_not_blindly_merged(self):
        t = self.t
        later = evidence.observe(t.store, dict(t.obs, observation_id="different-period", period_start=t.time(), period_end=t.time()))
        events.register_event(t.store, dict(t.event, event_id="later-event", revision_id="later-event-r1",
            observation_ids=[later["observation_id"]], independent_cluster_id="later-period"))
        self.assertEqual(len(t.store.all("economic_event_revision")), 2)


if __name__ == "__main__":
    unittest.main()
