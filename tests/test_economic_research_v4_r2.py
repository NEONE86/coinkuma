"""R2 acceptance tests: synthetic inputs, no market/performance assertions."""
import copy
from datetime import timedelta
from decimal import Decimal, localcontext
import unittest

import test_economic_research_v4_fixes as helpers
from test_economic_research_v4 import SOURCE, CONTRACT, policy
from one_trading.economic_research import evidence, events, review, theses
from one_trading.economic_research.contracts import question_contract
from one_trading.economic_research.semantics import numeric_identity
from one_trading.economic_research.store import digest


class R2Tests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.FixTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.t = self.h.t

    def other_asset(self):
        t = self.t
        contract = "0x" + "2" * 40
        sid = t.fetch(SOURCE, dict(contract=contract, value="100"))
        asset = copy.deepcopy(t.asset)
        asset.update(asset_id="eip155:1:" + contract, contract_address=contract, identity_evidence_ids=[sid])
        asset["exchange_symbol_mapping"]["BINANCE"].update(instrument="OTHERUSDT", evidence_ids=[sid])
        events.register_asset(t.store, asset)
        o = evidence.observe(t.store, dict(t.obs, observation_id="other", asset_id=asset["asset_id"],
            evidence_ids=[sid], locator=dict(snapshot_id=sid, json_pointer="/value")))
        events.register_event(t.store, dict(t.event, event_id="other", revision_id="other-r1", asset_id=asset["asset_id"],
            observation_ids=[o["observation_id"]], independent_cluster_id="other"))
        return asset["asset_id"]

    def multiasset_bundle(self, other):
        t = self.t
        packet = theses.make_packet(t.store, [t.aid, other])
        b = t.bundle(packet)
        b["thesis"].update(subject_asset_ids=packet["subject_asset_ids"], event_ids=packet["event_ids"])
        for f in b["forecasts"]:
            f["target_asset_id"] = t.aid
        self.h.numeric(b["forecasts"][-1], "200")
        return b

    def test_R201_numeric_alias_sum_rejected_correct_negative_label(self):
        t = self.t
        t.payload["forecasts"][0]["resolution_rule"].update(threshold="2", accept_source_claim=False)
        t.registered(); t.advance(days=181)
        first = evidence.observe(t.store, self.h.measurement("1"))
        initial = digest(first)
        t.advance(days=1)
        for i, value in enumerate((1, "1", "1.0", "1.00", "1e0")):
            alias = evidence.observe(t.store, dict(first, observation_id="alias" + str(i), value=value))
            self.assertEqual(first["available_at"], alias["available_at"])
            with self.assertRaisesRegex(ValueError, "duplicate_derived_fact"):
                evidence.observe(t.store, dict(first, observation_id="sum" + str(i), value="2",
                    epistemic_type="DERIVED_VALUE", input_observation_ids=["out", alias["observation_id"]],
                    calculation="SUM", derivation_version="sum-v1"))
        self.assertEqual(self.h.resolve(first)["outcome"], 0)
        result = review.score(t.store)
        self.assertEqual(Decimal(result["scores"][0]["mean_loss"]), Decimal(".64"))
        self.assertEqual([r["outcome"] for r in result["resolved_only_training_labels"]], [0])
        self.assertEqual(digest(t.store.get("observation", "out")), initial)

    def test_R201_exact_numeric_and_utc_identity_preserves_distinctions(self):
        t = self.t
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(numeric_identity("123456789012345678901234567890.0"), numeric_identity(123456789012345678901234567890))
            self.assertNotEqual(numeric_identity("123456789012345678901234567891"), numeric_identity(123456789012345678901234567890))
        self.assertEqual(numeric_identity("-0.00"), numeric_identity(0))
        for value in (True, 1.0, None, [1], Decimal("1"), "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                numeric_identity(value)
        base = evidence.observe(t.store, dict(t.obs, observation_id="period", period_start="2025-01-01T00:00:00Z", period_end="2025-01-02T00:00:00Z"))
        t.advance(days=1)
        alias = evidence.observe(t.store, dict(base, observation_id="offset", value=100,
            period_start="2025-01-01T09:00:00+09:00", period_end="2025-01-02T09:00:00+09:00"))
        identity = evidence.validate_observation(t.store, base)["fact_ids"]
        self.assertEqual(evidence.validate_observation(t.store, alias)["fact_ids"], identity)
        self.assertEqual(alias["available_at"], base["available_at"])
        other = self.other_asset()
        for changes in ({"asset_id": other}, {"unit": "TOKEN"}, {"metric": "other_metric"},
                        {"period_start": "2025-01-02T00:00:00Z", "period_end": "2025-01-03T00:00:00Z"}):
            o = evidence.observe(t.store, dict(base, **changes, observation_id="distinct-" + str(len(t.store.all("observation")))))
            self.assertNotEqual(evidence.validate_observation(t.store, o)["fact_ids"], identity)
        # Numeric JSON tokens (integer and fractional) are parsed exactly.
        for i, token in enumerate(("1", "1.000", '"1.00"')):
            sid = t.fetch(SOURCE, ('{"value":' + token + '}').encode())
            o = dict(t.obs, observation_id="raw" + str(i), value="1.0", evidence_ids=[sid], locator=dict(snapshot_id=sid, json_pointer="/value"))
            evidence.observe(t.store, o)

    def test_R201_alias_event_and_legacy_contamination_correction(self):
        t = self.t
        t.advance(seconds=1)
        alias = evidence.observe(t.store, dict(t.obs, observation_id="alias", value="100.0"))
        bad_event = dict(t.event, event_id="alias-event", revision_id="alias-event", observation_ids=["alias"], independent_cluster_id="fake")
        with self.assertRaisesRegex(ValueError, "duplicate_economic_event"):
            events.register_event(t.store, bad_event)
        self.assertEqual(len(t.store.all("economic_event_revision")), 1)
        t.payload["forecasts"][0]["resolution_rule"]["threshold"] = "2"
        t.registered(); t.advance(days=181)
        first = evidence.observe(t.store, self.h.measurement("1"))
        # Simulate byte-preserved pre-fix rows, never bypass production API in the
        # supported-input tests. Cached legacy fact IDs must not be trusted.
        alias = dict(first, observation_id="old-alias", value="1.0", fact_ids=["different-old-id"])
        t.store.put("observation", "old-alias", alias)
        total = dict(first, observation_id="bad-sum", value="2.0", epistemic_type="DERIVED_VALUE",
                     input_observation_ids=["out", "old-alias"], calculation="SUM", derivation_version="legacy")
        t.store.put("observation", "bad-sum", total)
        bad = dict(resolution_id="legacy-result", forecast_id="IMPLEMENTATION", status="RESOLVED", explanation="old",
            evidence_ids=first["evidence_ids"], observation_ids=["bad-sum"], supersedes_resolution_id=None,
            resolution_at=t.time(), outcome=1, measured_value="2.0")
        t.store.put("resolution", "legacy-result", bad)
        before = digest(bad)
        check = review.audit(t.store)
        self.assertEqual(check["status"], "ISSUES")
        self.assertIn("legacy-result", [r["record_id"] for r in check["semantics"]["checks"]["R2-01"]["issues"]])
        with self.assertRaisesRegex(ValueError, "duplicate_derived_fact"):
            review.score(t.store)
        fixed = self.h.resolve(first, key="corrected", parent="legacy-result")
        self.assertEqual(fixed["outcome"], 0)
        scored = review.score(t.store)
        self.assertEqual(Decimal(scored["scores"][0]["mean_loss"]), Decimal(".64"))
        self.assertEqual([x["outcome"] for x in scored["resolved_only_training_labels"]], [0])
        self.assertEqual(before, digest(t.store.get("resolution", "legacy-result")))

    def test_R201_legacy_alias_clock_and_cluster_audit(self):
        t = self.t
        t.advance(days=1)
        alias = dict(t.obs, observation_id="legacy-alias", value="100.0", available_at=t.time())
        t.store.put("observation", "legacy-alias", alias)
        event = dict(t.store.get("economic_event_revision", "event-r1"), event_id="alias", revision_id="alias-event",
                     observation_ids=["legacy-alias"], independent_cluster_id="fake")
        t.store.put("economic_event_revision", "alias-event", event)
        result = review.audit(t.store)["semantics"]["checks"]["R2-01"]
        self.assertEqual({r["record_id"] for r in result["issues"]}, {"legacy-alias", "alias-event"})
        t.packet = theses.make_packet(t.store, [t.aid])
        t.payload = t.bundle()
        t.payload["thesis"]["event_ids"] = t.packet["event_ids"]
        t.payload["decision"]["price_snapshot_id"] = t.ticker("100")["price_snapshot_id"]
        t.registered()
        with self.assertRaisesRegex(ValueError, "shared_facts"):
            review.score(t.store)

    def test_R202_multiasset_exact_target_correct_mae_and_revision_guard(self):
        t = self.t
        other = self.other_asset()
        b = self.multiasset_bundle(other)
        theses.register_bundle(t.store, b)
        f = copy.deepcopy(b["forecasts"][-1])
        f.update(forecast_id="other-price", question_id="other-price", target_asset_id=other)
        theses.register_forecast(t.store, f)
        with self.assertRaisesRegex(ValueError, "decision_price_forecast_target"):
            theses.register_decision(t.store, dict(b["decision"], decision_id="bad-decision", decision_series_id="bad",
                                                   forecast_ids=["other-price"]))
        changed = dict(b["forecasts"][-1], forecast_id="price-r2", supersedes_forecast_id="PRICE", target_asset_id=other)
        with self.assertRaisesRegex(ValueError, "question_contract_immutable"):
            theses.register_forecast(t.store, changed)
        t.advance(days=181)
        a = evidence.observe(t.store, self.h.measurement("50", fid="PRICE", key="a-price"))
        ob = dict(self.h.measurement("200", fid="other-price", key="b-price"), asset_id=other)
        ob = evidence.observe(t.store, ob)
        with self.assertRaisesRegex(ValueError, "measurement_identity"):
            self.h.resolve(ob, fid="PRICE", key="wrong-asset")
        self.h.resolve(a, fid="PRICE", key="actual-a")
        self.h.resolve(ob, fid="other-price", key="actual-b")
        losses = {Decimal(s["mean_loss"]) for s in review.score(t.store)["scores"]}
        self.assertEqual(losses, {Decimal(150), Decimal(0)})
        self.assertEqual(review.audit(t.store)["status"], "PASS")

    def test_R202_legacy_ambiguous_contract_and_composite_rejected(self):
        t = self.t
        other = self.other_asset()
        b = self.multiasset_bundle(other)
        b["forecasts"][0].pop("target_asset_id")
        with self.assertRaisesRegex(ValueError, "unambiguous_target"):
            theses.register_bundle(t.store, b)
        self.assertEqual(t.store.all("forecast"), [])
        theses.register_thesis(t.store, b["thesis"])
        for target in (None, [t.aid, other]):
            with self.assertRaisesRegex(ValueError, "unambiguous_target"):
                theses.register_forecast(t.store, dict(b["forecasts"][0], target_asset_id=target))
        with self.assertRaisesRegex(ValueError, "composite_measurement"):
            theses.register_forecast(t.store, dict(b["forecasts"][1], measurement_target={"formula": "A+B"}))
        legacy = dict(b["forecasts"][0], registered_at=t.time(), mode="PROSPECTIVE")
        t.store.put("forecast", legacy["forecast_id"], legacy)
        result = review.audit(t.store)["semantics"]["checks"]["R2-02"]
        self.assertEqual(result["status"], "ISSUES")
        self.assertIn("unambiguous_target", result["issues"][0]["reason"])

    def test_R203_independent_policy_end_to_end_revision_and_common_payoff(self):
        t = self.t
        # pair helper pre-registers both policies/change before the common packet.
        self.h.pair()
        root = copy.deepcopy(t.payload["forecasts"][-1])
        originals = {}
        for name, value in (("incumbent", "100"), ("challenger", "80")):
            f = self.h.numeric(copy.deepcopy(root), value)
            f.update(forecast_id=name + "-price", policy_id=name)
            originals[name] = theses.register_forecast(t.store, f)
            theses.register_decision(t.store, dict(t.payload["decision"], decision_id=name + "-independent", decision_series_id=name + "-independent",
                policy_version=name, forecast_ids=[name + "-price"]))
        ids = ["incumbent-independent", "challenger-independent"]
        before = digest(originals)
        paired = review.compare_policies(t.store, "change", ids)
        review.freeze_policy_cohort(t.store, dict(cohort_id="frozen", change_id="change", pair_ids=[paired["pair_id"]]))
        # Same policy cannot reset its root, branch, or claim the other parent.
        with self.assertRaisesRegex(ValueError, "parent_required"):
            theses.register_forecast(t.store, dict(originals["incumbent"], forecast_id="reset"))
        with self.assertRaisesRegex(ValueError, "revision_identity"):
            theses.register_forecast(t.store, dict(originals["challenger"], forecast_id="cross-policy", supersedes_forecast_id="incumbent-price"))
        with self.assertRaisesRegex(ValueError, "clock_must_advance"):
            theses.register_forecast(t.store, dict(originals["incumbent"], forecast_id="same-cutoff", supersedes_forecast_id="incumbent-price"))
        t.advance(days=30)
        update = self.h.updated_bundle()
        theses.register_thesis(t.store, update["thesis"])
        revised = copy.deepcopy(originals["incumbent"])
        revised.update(forecast_id="incumbent-r2", supersedes_forecast_id="incumbent-price", thesis_revision_id="thesis-r2",
                       knowledge_cutoff=update["forecasts"][0]["knowledge_cutoff"])
        self.h.numeric(revised, "90")
        theses.register_forecast(t.store, revised)
        selected = review.score(t.store, revision_policy="LATEST_AS_OF")["selected_forecasts"]
        self.assertIn("incumbent-r2", [f["forecast_id"] for f in selected])
        self.assertIn("challenger-price", [f["forecast_id"] for f in selected])
        t.advance(days=151)
        actual = evidence.observe(t.store, self.h.measurement("50", fid="PRICE"))
        self.h.resolve(actual, fid="incumbent-price")
        for mode, expected in (("INITIAL", 50), ("LATEST_AS_OF", 40)):
            result = review.score(t.store, revision_policy=mode)
            by_policy = {s["policy_id"]: Decimal(s["mean_loss"]) for s in result["scores"]}
            self.assertEqual(by_policy["incumbent"], expected)
            self.assertEqual(by_policy["challenger"], 30)
            self.assertEqual(result["counts"]["resolved_questions"], 1)
            self.assertEqual(result["counts"]["resolved_independent_event_clusters"], 1)
            self.assertEqual(len({r["resolution_id"] for r in result["resolved_only_training_labels"]}), 1)
        result = review.compare_policies(t.store, "change", ids)
        self.assertEqual([r["proposal_payoff"] for r in result["results"]["policies"]], ["-0.5", "-0.5"])
        self.assertEqual(review.compare_policies(t.store, "change", ids), result)
        self.assertEqual(before, digest({k: t.store.get("forecast", v["forecast_id"]) for k, v in originals.items()}))
        # A policy cannot choose a separate first outcome for the common question.
        with self.assertRaisesRegex(ValueError, "question_resolution_parent_required"):
            self.h.resolve(actual, fid="challenger-price", key="private-outcome")
        self.assertEqual(review.audit(t.store)["status"], "PASS")

    def test_R203_new_policy_cannot_enter_after_known_result(self):
        t = self.t
        review.register_policy(t.store, policy("early-policy"))
        t.payload["forecasts"][0]["resolution_rule"]["observation_mode"] = "EVER_IN_WINDOW"
        t.registered(); t.advance(days=30)
        o = self.h.measurement("1")
        o.update(period_start=t.time(), period_end=t.time())
        evidence.observe(t.store, o)
        f = dict(t.payload["forecasts"][0], forecast_id="new-policy", policy_id="early-policy")
        with self.assertRaisesRegex(ValueError, "already_known"):
            theses.register_forecast(t.store, f)
        review.register_policy(t.store, policy("late-policy"))
        with self.assertRaisesRegex(ValueError, "policy_must_precede"):
            theses.register_forecast(t.store, dict(f, policy_id="late-policy"))

    def test_R204_strict_booleans_at_registration_and_legacy_revalidation(self):
        t = self.t
        for field in ("accept_source_claim", "ignore_query_for_source_match", "outcome_already_observed"):
            for value in ("false", "true", 0, 1, None):
                b = copy.deepcopy(t.payload)
                target = b["forecasts"][0] if field == "outcome_already_observed" else b["forecasts"][0]["resolution_rule"]
                target[field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "boolean_required"):
                    theses.register_bundle(t.store, b)
                self.assertEqual(t.store.all("forecast"), [])
        # False rejects source claims; explicit true retains their provenance.
        t.payload["forecasts"][0]["resolution_rule"]["accept_source_claim"] = False
        t.registered(); t.advance(days=181)
        claim = evidence.observe(t.store, self.h.measurement("1", kind="SOURCE_CLAIM"))
        with self.assertRaisesRegex(ValueError, "source_claim_is_not"):
            self.h.resolve(claim)
        claim2 = evidence.observe(t.store, self.h.measurement("1", kind="SOURCE_CLAIM", fid="ECONOMICS", key="claim2"))
        self.h.resolve(claim2, fid="ECONOMICS")
        self.assertEqual(review.score(t.store)["resolved_only_training_labels"][0]["outcome_leaf_epistemic_types"], ["SOURCE_CLAIM"])
        old = copy.deepcopy(t.store.get("forecast", "IMPLEMENTATION"))
        old.update(forecast_id="legacy-invalid", question_id="legacy-invalid")
        old["resolution_rule"]["accept_source_claim"] = "false"
        t.store.put("forecast", "legacy-invalid", old)
        bad = dict(resolution_id="legacy-label", forecast_id="legacy-invalid", resolution_at=t.time(), status="RESOLVED",
                   observation_ids=["out"], evidence_ids=claim["evidence_ids"], outcome=1, supersedes_resolution_id=None)
        t.store.put("resolution", "legacy-label", bad)
        result = review.audit(t.store)["semantics"]["checks"]["R2-04"]
        self.assertEqual({r["record_id"] for r in result["issues"]}, {"legacy-invalid", "legacy-label"})
        with self.assertRaisesRegex(ValueError, "boolean_required"):
            review.score(t.store)


if __name__ == "__main__":
    unittest.main()
