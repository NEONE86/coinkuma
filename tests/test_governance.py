import dataclasses
import math
import sqlite3
import unittest

from one_trading.governance import (
    Costs,
    Evidence,
    GatePolicy,
    GovernanceStore,
    PositionRequest,
    Provenance,
    RiskPolicy,
    Stage,
    evaluate,
    size_position,
)


def valid_evidence(run_id="r1", strategy_id="s1", stage=Stage.OOS):
    return Evidence(
        run_id=run_id,
        strategy_id=strategy_id,
        stage=stage,
        dataset_sha256="a" * 64,
        generated_at="2025-12-01T00:00:00Z",
        window_start="2025-04-01T00:00:00Z",
        window_end="2025-10-01T00:00:00Z",
        train_end="2025-03-01T00:00:00Z",
        strategy_frozen_at="2025-03-02T00:00:00Z",
        holding_horizon_hours=168,
        embargo_hours=168,
        non_overlapping_samples=60,
        mean_reward_risk=3.5,
        net_expectancy_r=0.4,
        profit_factor=1.8,
        win_rate=0.55,
        max_drawdown_fraction=0.08,
        max_drawdown_r=8,
        costs=Costs(fees_bps=20, slippage_bps=10, funding_bps=0),
        costs_included=True,
        no_lookahead_checked=True,
        point_in_time_universe_checked=True,
        non_overlap_checked=True,
        drawdown_from_marked_equity=True,
        provenance=Provenance.MEASURED,
        measurement_artifact_sha256="b" * 64,
    )


def paper_evidence(run_id="p1", strategy_id="s1"):
    return dataclasses.replace(
        valid_evidence(run_id, strategy_id, Stage.PAPER),
        dataset_sha256="c" * 64,
        window_start="2025-10-02T00:00:00Z",
        window_end="2025-11-30T00:00:00Z",
    )


def valid_position():
    return PositionRequest(
        equity=100_000,
        entry_price=100,
        stop_price=95,
        target_price=122,
        day_start_equity=100_000,
        high_water_equity=100_000,
        roundtrip_cost_fraction=0.002,
    )


class EvidenceTests(unittest.TestCase):
    def test_complete_evidence_only_provisionally_passes(self):
        result = evaluate(valid_evidence())
        self.assertTrue(result.passed)
        self.assertEqual(result.status, "PROVISIONAL_PASS")
        self.assertIn("not prove predictive edge", result.disclaimer)
        self.assertIn("caller-attested", result.disclaimer)

    def test_backtest_never_qualifies(self):
        self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), stage=Stage.BACKTEST)).passed)

    def test_all_required_metrics_fail_closed(self):
        for name in (
            "non_overlapping_samples", "mean_reward_risk", "net_expectancy_r", "profit_factor", "win_rate",
            "max_drawdown_fraction", "max_drawdown_r", "holding_horizon_hours", "embargo_hours",
        ):
            for value in (None, float("nan"), float("inf"), True):
                with self.subTest(name=name, value=value):
                    self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), **{name: value})).passed)

    def test_missing_provenance_or_unchecked_controls_fail(self):
        for name in (
            "costs_included", "no_lookahead_checked", "point_in_time_universe_checked", "non_overlap_checked", "drawdown_from_marked_equity",
        ):
            self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), **{name: None})).passed)
        self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), provenance=Provenance.SELF_REPORTED)).passed)
        self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), measurement_artifact_sha256=None)).passed)

    def test_proposed_metric_thresholds(self):
        for name, value in {
            "mean_reward_risk": 2.99, "net_expectancy_r": 0.249, "profit_factor": 1.49,
            "win_rate": -0.01, "non_overlapping_samples": 49, "max_drawdown_fraction": 0.151,
            "max_drawdown_r": 12.1,
        }.items():
            with self.subTest(name=name):
                self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), **{name: value})).passed)

    def test_explicit_zero_funding_is_accepted_missing_is_not(self):
        self.assertTrue(evaluate(valid_evidence()).passed)
        self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), costs=Costs(20, 10, None))).passed)

    def test_timestamps_and_embargo_are_validated(self):
        for replacement in (
            {"window_start": "2025-04-01"},
            {"window_end": "2026-12-01T00:00:00Z"},
            {"generated_at": "2100-01-01T00:00:00Z"},
            {"train_end": "2025-04-01T00:00:00Z"},
            {"strategy_frozen_at": "2025-04-01T00:00:00Z"},
            {"embargo_hours": 167},
            {"train_end": "2025-03-25T00:00:00Z", "strategy_frozen_at": "2025-03-26T00:00:00Z"},
            {"embargo_hours": 1e300},
        ):
            with self.subTest(replacement=replacement):
                self.assertFalse(evaluate(dataclasses.replace(valid_evidence(), **replacement)).passed)

    def test_policy_cannot_be_nonfinite(self):
        self.assertFalse(evaluate(valid_evidence(), GatePolicy(min_profit_factor=float("nan"))).passed)

    def test_json_roundtrip(self):
        evidence = valid_evidence()
        self.assertEqual(Evidence.from_dict(dataclasses.asdict(evidence)), evidence)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = GovernanceStore()
        self.store.register_strategy("s1", {"sma": 20})

    def tearDown(self):
        self.store.close()

    def test_strategy_and_run_ids_cannot_be_overwritten(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.register_strategy("s1", {"sma": 30})
        self.store.record_evidence(valid_evidence())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_evidence(valid_evidence())
        self.assertEqual(self.store.status()["evidence_runs"], 1)
        self.assertEqual(self.store.status()["attempts"], 1)

    def test_sql_update_and_delete_are_blocked(self):
        self.store.record_evidence(valid_evidence())
        self.store.propose_promotion("s1")
        for table in ("strategies", "evidence", "attempts", "proposals"):
            for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET recorded_at='changed'" if table != "strategies" else "UPDATE strategies SET registered_at='changed'"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(sql)
                self.store.connection.rollback()

    def test_losing_variants_remain_visible(self):
        result = self.store.record_evidence(dataclasses.replace(valid_evidence(), net_expectancy_r=-0.2))
        self.assertFalse(result.passed)
        self.assertEqual(self.store.status()["attempts"], 1)
        outcome = self.store.connection.execute("SELECT outcome FROM attempts").fetchone()[0]
        self.assertEqual(outcome, "REJECTED")

    def test_variant_budget_is_bounded(self):
        with GovernanceStore(policy=GatePolicy(max_variants_per_experiment=2)) as store:
            store.register_strategy("a", {})
            store.register_strategy("b", {})
            with self.assertRaises(ValueError):
                store.register_strategy("c", {})

    def test_backtest_or_oos_alone_cannot_propose(self):
        self.store.record_evidence(valid_evidence())
        proposal = self.store.propose_promotion("s1")
        self.assertEqual(proposal["status"], "REJECTED")
        self.assertIn("missing_stage:PAPER", proposal["reasons"])

    def test_two_stages_only_create_human_review_proposal(self):
        self.store.record_evidence(valid_evidence())
        self.store.record_evidence(paper_evidence())
        proposal = self.store.propose_promotion("s1")
        self.assertEqual(proposal["status"], "HUMAN_REVIEW_REQUIRED")
        self.assertFalse(proposal["live_trading_enabled"])
        self.assertFalse(self.store.status()["live_trading_enabled"])

    def test_latest_failed_run_cannot_be_hidden_by_backfilling_success(self):
        self.store.record_evidence(valid_evidence())
        self.store.record_evidence(dataclasses.replace(paper_evidence("p-new"), net_expectancy_r=-0.5))
        self.store.record_evidence(dataclasses.replace(paper_evidence("p-old"), window_end="2025-11-01T00:00:00Z"))
        proposal = self.store.propose_promotion("s1")
        self.assertEqual(proposal["status"], "REJECTED")
        self.assertEqual(proposal["run_ids"]["PAPER"], "p-new")

    def test_overlapping_stages_do_not_qualify(self):
        self.store.record_evidence(valid_evidence())
        self.store.record_evidence(dataclasses.replace(paper_evidence(), window_start="2025-09-01T00:00:00Z"))
        self.assertIn("paper_not_strictly_after_oos", self.store.propose_promotion("s1")["reasons"])

    def test_challenger_must_improve_comparable_champion(self):
        self.store.register_strategy("s2", {"sma": 25})
        self.store.record_evidence(valid_evidence())
        self.store.record_evidence(paper_evidence())
        self.store.record_evidence(valid_evidence("r2", "s2"))
        self.store.record_evidence(paper_evidence("p2", "s2"))
        proposal = self.store.propose_promotion("s2", "s1")
        self.assertIn("insufficient_challenger_improvement", proposal["reasons"])
        self.store.record_evidence(dataclasses.replace(paper_evidence("p3", "s2"), net_expectancy_r=0.5))
        self.assertEqual(self.store.propose_promotion("s2", "s1")["status"], "HUMAN_REVIEW_REQUIRED")

    def test_missing_strategy_and_foreign_keys_fail(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_evidence(valid_evidence(strategy_id="missing"))
        with self.assertRaises(KeyError):
            self.store.propose_promotion("missing")

    def test_explicit_failed_attempt_can_be_recorded(self):
        self.store.record_attempt("s1", "FAILED", {"error": "provider gap"})
        self.assertEqual(self.store.status()["attempts"], 1)


class RiskTests(unittest.TestCase):
    def test_formula_includes_roundtrip_costs(self):
        proposal = size_position(valid_position())
        self.assertTrue(proposal.allowed)
        self.assertAlmostEqual(proposal.notional, 100_000 * 0.005 / (0.05 + 0.002))
        self.assertAlmostEqual(proposal.modeled_stop_loss, 500)
        self.assertAlmostEqual(proposal.quantity, proposal.notional / 100)
        self.assertEqual(proposal.mode, "SIMULATION_ONLY")

    def test_notional_cap_and_aggregate_risk_room(self):
        proposal = size_position(dataclasses.replace(valid_position(), max_notional=1_000))
        self.assertTrue(proposal.allowed)
        self.assertEqual(proposal.notional, 1_000)
        proposal = size_position(dataclasses.replace(valid_position(), existing_open_stop_risk_fraction=0.029))
        self.assertTrue(proposal.allowed)
        self.assertAlmostEqual(proposal.risk_fraction, 0.001)

    def test_circuit_breakers(self):
        for replacement, reason in (
            ({"equity": 97_000}, "daily_marked_equity_loss_limit"),
            ({"equity": 85_000, "day_start_equity": 85_000}, "marked_equity_drawdown_limit"),
            ({"existing_open_stop_risk_fraction": 0.03}, "aggregate_open_stop_risk_limit"),
            ({"existing_gross_notional": 100_000}, "aggregate_notional_limit"),
            ({"requested_risk_fraction": 0.006}, "per_trade_risk_cap_exceeded"),
        ):
            with self.subTest(reason=reason):
                result = size_position(dataclasses.replace(valid_position(), **replacement))
                self.assertFalse(result.allowed)
                self.assertIn(reason, result.reasons)
                self.assertEqual(result.notional, 0)

    def test_short_prices_are_directional(self):
        self.assertTrue(size_position(dataclasses.replace(valid_position(), side="SHORT", stop_price=105, target_price=78)).allowed)
        self.assertFalse(size_position(dataclasses.replace(valid_position(), side="SHORT")).allowed)

    def test_all_nonfinite_fields_and_nonsensical_prices_fail(self):
        for name, value in dataclasses.asdict(valid_position()).items():
            if name in {"side", "max_notional"}:
                continue
            for bad in (float("nan"), float("inf"), None, True):
                with self.subTest(name=name, bad=bad):
                    self.assertFalse(size_position(dataclasses.replace(valid_position(), **{name: bad})).allowed)
        for replacement in ({"stop_price": 100}, {"target_price": 90}, {"entry_price": 0}, {"equity": -1}, {"roundtrip_cost_fraction": -0.1}):
            self.assertFalse(size_position(dataclasses.replace(valid_position(), **replacement)).allowed)

    def test_net_target_reward_risk_not_gross(self):
        self.assertFalse(size_position(dataclasses.replace(valid_position(), target_price=115)).allowed)

    def test_invalid_policy_fails_closed(self):
        self.assertFalse(size_position(valid_position(), RiskPolicy(max_drawdown_fraction=float("nan"))).allowed)


if __name__ == "__main__":
    unittest.main()
