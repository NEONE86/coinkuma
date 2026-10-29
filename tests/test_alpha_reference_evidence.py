from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import unittest

from one_trading.alpha_loop.reference.evidence import EvidenceSpec, EvidenceLedger, MixtureEvidence, Metrics
from one_trading.alpha_loop.reference.voi import decision_value, evaluate_experiment


def specification(**kwargs):
    base = dict(experiment_id="synthetic-1", version="v1", alpha_index=1,
                registered_at="2026-10-29T00:00:00Z", mode="SYNTHETIC",
                policy="AI", baseline="PRICE", risk_fingerprint="matched-risk-v1",
                return_scale=0.01, minimum_clusters=3, max_unit_loss=0.10)
    base.update(kwargs)
    return EvidenceSpec(**base)


def case_parameters(ledger, i):
    start = datetime(2026, 10, 30, tzinfo=timezone.utc) + timedelta(minutes=2 * i)
    return dict(case_id=f"case-{i}", cluster_id=f"cluster-{i}",
                decision_at=(start - timedelta(seconds=10)).isoformat(),
                available_at=(start - timedelta(seconds=20)).isoformat(),
                starts_at=start.isoformat(), due_at=(start + timedelta(minutes=1)).isoformat(),
                decision_hashes={p: f"{p}-{i}-frozen" for p in ledger.spec.policies},
                risk_fingerprint=ledger.spec.risk_fingerprint, spec_hash=ledger.spec.fingerprint)


def settle(ledger, i, own=0.01, baseline=0.0):
    case = next(c for c in ledger.cases if c["case_id"] == f"case-{i}")
    metrics = {ledger.spec.policy: Metrics(own, 0), ledger.spec.baseline: Metrics(baseline, 0)}
    if ledger.spec.memory_comparator:
        metrics[ledger.spec.memory_comparator] = Metrics(0, 0)
    as_of = max(case["due_at"], ledger.evaluation_as_of)
    return ledger.resolve(case_id=case["case_id"], metrics=metrics,
                          received_at=case["due_at"], as_of=as_of,
                          source_hash=f"synthetic-source-{i}", mode=ledger.spec.mode)


class SequentialEvidenceTests(unittest.TestCase):
    def test_exact_product_against_decimal(self):
        model = MixtureEvidence((0.1, 0.5))
        seq = [1, -1, 0.3, 0, -0.7]
        expected = []
        for lam in (Decimal("0.1"), Decimal("0.5")):
            product = Decimal(1)
            for x in seq:
                product *= 1 + lam * Decimal(str(x))
            expected.append(product)
        for x in seq:
            model.update(x)
        self.assertAlmostEqual(math.exp(model.log_e), float(sum(expected) / 2), places=14)

    def test_alpha_schedule_telescope(self):
        total = sum(specification(alpha_index=i).alpha for i in range(1, 1001))
        self.assertAlmostEqual(total, .05 * (1 - 1 / 1001), places=14)

    def test_nonfinite_inputs_rejected(self):
        for x in (float("nan"), float("inf"), True):
            with self.subTest(x=x), self.assertRaises(ValueError):
                Metrics(x, 0)

    def test_lambda_out_of_range(self):
        for value in (0, 1, -1, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                specification(lambda_grid=(value,))

    def test_spec_hash_changes_with_version(self):
        a = specification()
        self.assertNotEqual(a.fingerprint, replace(a, version="v2").fingerprint)

    def test_frozen_spec(self):
        with self.assertRaises(AttributeError):
            specification().version = "v2"

    def test_duplicate_case_idempotent(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        self.assertEqual(ledger.register_case(**p), ledger.register_case(**p))
        self.assertEqual(len(ledger.cases), 1)

    def test_duplicate_cluster_rejected(self):
        ledger = EvidenceLedger(specification())
        ledger.register_case(**case_parameters(ledger, 0))
        p = case_parameters(ledger, 1)
        p["cluster_id"] = "cluster-0"
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_overlap_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        ledger.register_case(**p)
        p.update(case_id="new", cluster_id="new")
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_future_input_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        p["available_at"] = p["due_at"]
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_decision_after_entry_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        p["decision_at"] = p["starts_at"]
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_version_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        p["spec_hash"] = "different"
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_unmatched_risk_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        p["risk_fingerprint"] = "more-leverage"
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_missing_comparator_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        p["decision_hashes"].pop("PRICE")
        with self.assertRaises(ValueError):
            ledger.register_case(**p)

    def test_immature_label_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        ledger.register_case(**p)
        with self.assertRaises(ValueError):
            ledger.resolve(case_id="case-0", metrics={"AI": Metrics(.01, 0), "PRICE": Metrics(0, 0)},
                           received_at=p["starts_at"], as_of=p["due_at"], source_hash="s", mode="SYNTHETIC")

    def test_mixed_origins_rejected(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        ledger.register_case(**p)
        with self.assertRaises(ValueError):
            ledger.resolve(case_id="case-0", metrics={"AI": Metrics(.01, 0), "PRICE": Metrics(0, 0)},
                           received_at=p["due_at"], as_of=p["due_at"], source_hash="s", mode="FORWARD_PAPER")

    def test_partial_label_does_not_score(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        ledger.register_case(**p)
        with self.assertRaises(ValueError):
            ledger.resolve(case_id="case-0", metrics={"AI": Metrics(.01, 0)},
                           received_at=p["due_at"], as_of=p["due_at"], source_hash="s", mode="SYNTHETIC")
        self.assertEqual(ledger.receipt()["scored_clusters"], 0)

    def test_late_label_cannot_selectively_skip(self):
        ledger = EvidenceLedger(specification())
        for i in range(2):
            ledger.register_case(**case_parameters(ledger, i))
        settle(ledger, 1)
        self.assertEqual(ledger.receipt()["scored_clusters"], 0)
        self.assertEqual(ledger.receipt()["resolved_waiting_for_prior"], 1)
        settle(ledger, 0)
        self.assertEqual(ledger.receipt()["scored_clusters"], 2)

    def test_evaluation_clock_reversal_rejected(self):
        ledger = EvidenceLedger(specification())
        for i in range(2):
            ledger.register_case(**case_parameters(ledger, i))
        settle(ledger, 1)
        p = ledger.cases[0]
        with self.assertRaises(ValueError):
            ledger.resolve(case_id="case-0", metrics={"AI": Metrics(.01, 0), "PRICE": Metrics(0, 0)},
                           received_at=p["due_at"], as_of=p["due_at"], source_hash="s", mode="SYNTHETIC")
        self.assertEqual(ledger.receipt()["scored_clusters"], 0)

    def test_no_backdating_new_case_after_observing_outcomes(self):
        ledger = EvidenceLedger(specification())
        ledger.register_case(**case_parameters(ledger, 0))
        p = ledger.cases[0]
        ledger.resolve(case_id="case-0", metrics={"AI": Metrics(.01, 0), "PRICE": Metrics(0, 0)},
                       received_at=p["due_at"], as_of="2026-11-30T00:00:00Z", source_hash="s", mode="SYNTHETIC")
        with self.assertRaises(ValueError):
            ledger.register_case(**case_parameters(ledger, 1))

    def test_tail_guard_sees_loss_behind_missing_label(self):
        ledger = EvidenceLedger(specification())
        for i in range(2):
            ledger.register_case(**case_parameters(ledger, i))
        settle(ledger, 1, own=-.99)
        r = ledger.receipt()
        self.assertEqual(r["scored_clusters"], 0)
        self.assertEqual(r["state"], "RAW_TAIL_LIMIT_BREACHED")
        self.assertAlmostEqual(r["raw_net_sum_not_compounded"], -.99)

    def test_outcome_duplicate_and_conflict(self):
        ledger = EvidenceLedger(specification())
        ledger.register_case(**case_parameters(ledger, 0))
        self.assertEqual(settle(ledger, 0), settle(ledger, 0))
        with self.assertRaises(ValueError):
            settle(ledger, 0, own=.02)
        self.assertEqual(ledger.receipt()["scored_clusters"], 1)

    def test_synthetic_threshold_is_not_market_proof(self):
        ledger = EvidenceLedger(specification())
        for i in range(60):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i)
        r = ledger.receipt()
        self.assertTrue(r["statistical_gate_passed"])
        self.assertEqual(r["state"], "SYNTHETIC_THRESHOLD_DEMO")
        self.assertFalse(r["edge_proven"])
        self.assertFalse(r["live_trading_enabled"])

    def test_no_edge_against_identical_policy(self):
        ledger = EvidenceLedger(specification())
        for i in range(60):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i, own=.01, baseline=.01)
        r = ledger.receipt()
        self.assertFalse(r["statistical_gate_passed"])
        self.assertAlmostEqual(r["streams"]["vs_baseline"]["log_e"], 0.0)

    def test_hurdle_and_delta_are_bounded(self):
        ledger = EvidenceLedger(specification(absolute_hurdle=.9, incremental_hurdle=.9))
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0, own=-.01, baseline=.01)
        self.assertEqual(ledger.raw[0]["increments"]["vs_baseline"], -1)
        self.assertEqual(ledger.raw[0]["increments"]["absolute"], -1)

    def test_memory_gate_required_when_claimed(self):
        ledger = EvidenceLedger(specification(memory_comparator="SEMANTIC"))
        for i in range(60):
            p = case_parameters(ledger, i)
            ledger.register_case(**p)
            ledger.resolve(case_id=p["case_id"], metrics={"AI": Metrics(.01, 0),
                "PRICE": Metrics(0, 0), "SEMANTIC": Metrics(.01, 0)},
                received_at=p["due_at"], as_of=p["due_at"], source_hash=str(i), mode="SYNTHETIC")
        self.assertFalse(ledger.receipt()["statistical_gate_passed"])
        self.assertAlmostEqual(ledger.receipt()["streams"]["memory_increment"]["log_e"], 0)

    def test_historical_never_forward(self):
        ledger = EvidenceLedger(specification(mode="HISTORICAL"))
        for i in range(60):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i)
        self.assertEqual(ledger.receipt()["state"], "HISTORICAL_EXPLORATION_ONLY")

    def test_costs_not_ignored(self):
        ledger = EvidenceLedger(specification())
        p = case_parameters(ledger, 0)
        ledger.register_case(**p)
        ledger.resolve(case_id=p["case_id"], metrics={"AI": Metrics(.01, .008, .004), "PRICE": Metrics(0, 0)},
                       received_at=p["due_at"], as_of=p["due_at"], source_hash="s", mode="SYNTHETIC")
        self.assertLess(ledger.receipt()["streams"]["absolute"]["log_e"], 0)


class InformationValueTests(unittest.TestCase):
    prior = {"works": .5, "fails": .5}
    actions = {"NO_TRADE": {"works": 0, "fails": 0}, "BUY": {"works": 2, "fails": -2}}

    def test_zero_expected_value_prefers_no_trade(self):
        self.assertEqual(decision_value(self.prior, self.actions)["action"], "NO_TRADE")

    def test_perfect_information_hand_calculation(self):
        r = evaluate_experiment(prior=self.prior, actions=self.actions,
            likelihoods={"works": {"yes": 1, "no": 0}, "fails": {"yes": 0, "no": 1}}, experiment_cost=.1)
        self.assertAlmostEqual(r["evsi"], 1)
        self.assertAlmostEqual(r["net_decision_value"], .9)

    def test_no_information_not_worth_cost(self):
        r = evaluate_experiment(prior=self.prior, actions=self.actions,
            likelihoods={"works": {"yes": .5, "no": .5}, "fails": {"yes": .5, "no": .5}}, experiment_cost=.1)
        self.assertAlmostEqual(r["evsi"], 0)
        self.assertFalse(r["candidate_for_research"])

    def test_expensive_information_rejected(self):
        r = evaluate_experiment(prior=self.prior, actions=self.actions,
            likelihoods={"works": {"yes": 1, "no": 0}, "fails": {"yes": 0, "no": 1}}, experiment_cost=2)
        self.assertFalse(r["candidate_for_research"])

    def test_unknown_probabilities_not_fabricated(self):
        with self.assertRaises(ValueError):
            decision_value({"works": .9, "fails": .9}, self.actions)

    def test_bad_conditional_probability_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_experiment(prior=self.prior, actions=self.actions,
                likelihoods={"works": {"yes": 1, "no": 1}, "fails": {"yes": 0, "no": 1}}, experiment_cost=.1)

    def test_no_trade_must_exist(self):
        with self.assertRaises(ValueError):
            decision_value(self.prior, {"BUY": self.actions["BUY"]})

    def test_inputs_are_labelled_assumptions(self):
        r = evaluate_experiment(prior=self.prior, actions=self.actions,
            likelihoods={"works": {"yes": .8, "no": .2}, "fails": {"yes": .2, "no": .8}}, experiment_cost=.1)
        self.assertEqual(r["input_status"], "ASSUMED")
        self.assertFalse(r["live_trading_enabled"])

    def test_combined_cost_overflow_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_experiment(prior=self.prior, actions=self.actions,
                likelihoods={"works": {"yes": 1, "no": 0}, "fails": {"yes": 0, "no": 1}},
                experiment_cost=1e308, delay_cost=1e308)

    def test_large_uninformative_payoffs_have_exact_zero_information_value(self):
        prior = {"a": .1, "b": .2, "c": .3, "d": .4}
        actions = {"NO_TRADE": dict.fromkeys(prior, 0),
                   "KEEP": {"a": 1e12, "b": 2e12, "c": 3e12, "d": 4e12}}
        r = evaluate_experiment(prior=prior, actions=actions,
            likelihoods={k: {"L": .2, "R": .8} for k in prior}, experiment_cost=0)
        self.assertEqual(r["evsi"], 0)
        self.assertFalse(r["candidate_for_research"])


if __name__ == "__main__":
    unittest.main()
