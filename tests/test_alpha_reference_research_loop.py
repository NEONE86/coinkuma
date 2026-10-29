"""Synthetic unit tests: control guarantees, not investment-performance evidence."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from one_trading.alpha_loop.reference.research_loop import ControlError, ExperimentSpec, ResearchLoop, demo, digest, stamp

UTC = timezone.utc


class ResearchLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="one-loop-tests-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "control.sqlite"
        self.time = datetime(2026, 10, 29, tzinfo=UTC)
        self.budgets = {"CF": {"cash_units": 100, "time_seconds": 100},
                        "GROWTH": {"cash_units": 100, "time_seconds": 100}}
        self.loop = ResearchLoop(self.path, self.budgets, lambda: self.time)
        self.addCleanup(lambda: self.loop.close())
        self.h = digest({"synthetic": "one"})
        self.other_h = digest({"synthetic": "two"})

    def spec(self, eid="e1", engine="CF", **kwargs):
        values = dict(id=eid, lineage=eid, engine=engine, version=1,
                      preregistered_at=stamp(self.time), deadline_at=stamp(self.time + timedelta(days=1)),
                      cash_budget_units=100, time_budget_seconds=100, evidence_mode="SYNTHETIC")
        values.update(kwargs)
        return ExperimentSpec(**values)

    def ready(self, eid="e1", engine="CF", **kwargs):
        self.loop.register_experiment(self.spec(eid, engine, **kwargs))
        self.loop.record_data(eid, self.h)
        return eid

    def forward(self, eid="e1", engine="CF", **kwargs):
        self.ready(eid, engine, **kwargs)
        self.time += timedelta(seconds=1)
        self.loop.begin_forward(eid)
        return eid

    def enqueue(self, key="t1", eid="e1", kind="research", **kwargs):
        values = dict(input_hash=self.h, cash_units=10, time_seconds=10, decision_value_score=1)
        values.update(kwargs)
        return self.loop.enqueue_task(key, eid, kind, **values)

    def ack(self, task, **kwargs):
        values = dict(result={"synthetic": True}, actual_cash_units=task["task"]["cash_units"],
                      actual_time_seconds=task["task"]["time_seconds"])
        values.update(kwargs)
        return self.loop.ack(task["task_key"], task["claim_token"], **values)

    def test_spec_frozen_and_idempotent_registration(self):
        spec = self.spec()
        with self.assertRaises(FrozenInstanceError):
            spec.version = 2
        self.loop.register_experiment(spec)
        self.loop.register_experiment(spec)
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 1)
        with self.assertRaises(ControlError):
            self.loop.register_experiment(replace(spec, cash_budget_units=90))

    def test_duplicate_lineage_version_is_not_new_hypothesis(self):
        spec = self.spec()
        self.loop.register_experiment(spec)
        with self.assertRaises(ControlError):
            self.loop.register_experiment(replace(spec, id="renamed"))

    def test_utc_and_unknown_budgets_rejected(self):
        with self.assertRaises(ControlError):
            self.spec(preregistered_at="2026-10-29T00:00:00")
        with self.assertRaises(ControlError):
            self.spec(cash_budget_units=None)
        with self.assertRaises(ControlError):
            self.loop.register_experiment(self.spec(engine="UNFUNDED"))

    def test_forward_requires_later_actual_registration(self):
        self.ready()
        with self.assertRaises(ControlError):
            self.loop.begin_forward("e1")
        self.time += timedelta(seconds=1)
        self.loop.begin_forward("e1")
        self.assertEqual(self.loop.status("e1")["state"], "FORWARD_TEST")

    def test_historical_cannot_be_promoted_to_forward(self):
        self.ready(evidence_mode="HISTORICAL")
        self.time += timedelta(seconds=1)
        with self.assertRaises(ControlError):
            self.loop.begin_forward("e1")
        self.assertIsNone(self.loop.status("e1")["alpha"])

    def test_alpha_global_immutable_and_not_recycled(self):
        self.forward()
        first = self.loop.begin_forward("e1")
        self.loop.record_evidence_verdict("e1", "reject", "synthetic-rejection", verified_receipt=True)
        self.forward("g1", "GROWTH")
        second = self.loop.status("g1")["alpha"]
        self.assertEqual((first["j"], second["j"]), (1, 2))
        self.assertAlmostEqual(first["alpha"], .025)
        self.assertAlmostEqual(second["alpha"], .05 / 6)
        with self.assertRaises(sqlite3.IntegrityError):
            self.loop.db.execute("DELETE FROM alpha_registry WHERE j=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.loop.db.execute("UPDATE alpha_registry SET alpha=1 WHERE j=2")

    def test_duplicate_data_is_not_fresh_and_heartbeat_is_free(self):
        self.ready()
        count = self.loop.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.assertFalse(self.loop.record_data("e1", self.h))
        for _ in range(10):
            self.assertIsNone(self.loop.tick())
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], count)
        self.assertEqual(self.loop.status("e1")["observations"], 1)
        self.assertEqual(self.loop.engine_status("CF")["cash_spent"], 0)

    def test_task_key_and_input_deduplication(self):
        self.ready()
        self.enqueue()
        self.enqueue()
        with self.assertRaises(ControlError):
            self.enqueue(cash_units=11)
        with self.assertRaises(ControlError):
            self.enqueue(key="same-data-new-key")
        with self.assertRaises(ControlError):
            self.enqueue(key="new-slot", slot="cheat")

    def test_unknown_task_cost_and_unseen_data_rejected(self):
        self.ready()
        for kwargs in ({"cash_units": None}, {"time_seconds": None}, {"cash_units": -1},
                       {"cash_units": True}, {"decision_value_score": float("nan")},
                       {"input_hash": self.other_h}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ControlError):
                self.enqueue(**kwargs)

    def test_claim_reserves_and_unacknowledged_work_cannot_overspend(self):
        self.ready(cash_budget_units=15, time_budget_seconds=15)
        self.enqueue()
        self.loop.record_data("e1", self.other_h)
        self.enqueue("t2", input_hash=self.other_h)
        first = self.loop.tick()
        self.assertIsNotNone(first)
        self.assertEqual(self.loop.status("e1")["cash_reserved"], 10)
        self.assertIsNone(self.loop.tick())
        self.ack(first, actual_cash_units=5, actual_time_seconds=5)
        self.assertIsNotNone(self.loop.tick())
        self.assertEqual(self.loop.status("e1")["cash_spent"] + self.loop.status("e1")["cash_reserved"], 15)

    def test_engine_budget_independent_and_blocked_experiment_does_not_block(self):
        self.ready(cash_budget_units=1000)
        self.enqueue(cash_units=101)
        self.ready("blocked")
        self.enqueue("blocked-work", eid="blocked")
        self.loop.suspend("blocked", "DATA_BLOCKED", "synthetic unavailable source")
        self.ready("g1", "GROWTH")
        self.enqueue("growth-work", eid="g1")
        self.assertEqual(self.loop.tick()["task_key"], "growth-work")
        self.assertEqual(self.loop.engine_status("CF")["cash_reserved"], 0)
        self.assertEqual(self.loop.engine_status("GROWTH")["cash_reserved"], 10)

    def test_due_label_first_then_falsifier_then_research(self):
        self.forward()
        self.enqueue("research", decision_value_score=999)
        self.enqueue("falsifier", kind="cheap_falsifier")
        self.enqueue("label", kind="due_label", cash_units=0, time_seconds=0,
                     due_at=stamp(self.time), slot="forecast/6h")
        order = []
        for _ in range(3):
            task = self.loop.tick()
            order.append(task["task_key"])
            self.ack(task)
        self.assertEqual(order, ["label", "falsifier", "research"])

    def test_not_yet_matured_label_cannot_run(self):
        self.forward()
        self.enqueue("future", kind="due_label", due_at=stamp(self.time + timedelta(hours=6)), slot="f/6h")
        self.assertIsNone(self.loop.tick())
        self.time += timedelta(hours=6)
        self.assertEqual(self.loop.tick()["task_key"], "future")

    def test_forward_label_cannot_claim_pre_activation_maturity(self):
        self.forward()
        with self.assertRaises(ControlError):
            self.enqueue("past", kind="due_label", due_at=stamp(self.time - timedelta(seconds=1)), slot="f/6h")

    def test_zero_cost_labels_survive_research_budget_exhaustion(self):
        self.forward(cash_budget_units=10, time_budget_seconds=10)
        self.enqueue()
        self.ack(self.loop.tick())
        self.enqueue("mandatory", kind="due_label", cash_units=0, time_seconds=0,
                     due_at=stamp(self.time), slot="forecast/24h")
        self.assertEqual(self.loop.tick()["task_key"], "mandatory")

    def test_stable_tie_break_and_cost_value_heuristic(self):
        self.ready()
        self.enqueue("b", decision_value_score=1)
        self.loop.record_data("e1", self.other_h)
        self.enqueue("a", input_hash=self.other_h, decision_value_score=1)
        self.assertEqual(self.loop.tick()["task_key"], "a")
        self.assertEqual(self.loop.get_task("a")["task"]["score_semantics"], "HEURISTIC_NOT_ESTIMATED_TRUE_EV")

    def test_ack_idempotent_and_claim_token_protected(self):
        self.ready()
        self.enqueue()
        task = self.loop.tick()
        with self.assertRaises(ControlError):
            self.loop.ack("t1", "wrong", result={}, actual_cash_units=0, actual_time_seconds=0)
        self.assertEqual(self.loop.engine_status("CF")["cash_reserved"], 10)
        self.ack(task)
        self.ack(task)
        self.assertEqual(self.loop.engine_status("CF")["cash_spent"], 10)
        with self.assertRaises(ControlError):
            self.ack(task, actual_cash_units=9)

    def test_actual_overrun_is_durable_and_freezes_engine_without_extra_budget(self):
        self.forward()
        self.enqueue()
        task = self.loop.tick()
        with self.assertRaises(ControlError):
            self.ack(task, actual_cash_units=12)
        status = self.loop.engine_status("CF")
        self.assertEqual(status["engine_halted"], 1)
        self.assertEqual(status["cash_limit"], 100)
        self.assertEqual(status["cash_reserved"], 10)
        self.assertEqual(status["unreconciled_overruns"][0]["actual_cash_units"], 12)
        self.assertEqual(self.loop.get_task("t1")["state"], "CLAIMED")
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM audit WHERE action='OVER_BUDGET_RECEIPT'").fetchone()[0], 1)
        with self.assertRaises(ControlError):
            self.ack(task, actual_cash_units=9)
        self.loop.close()
        self.loop = ResearchLoop(self.path, self.budgets, lambda: self.time)
        self.assertEqual(self.loop.engine_status("CF")["engine_halted"], 1)
        self.assertEqual(self.loop.engine_status("CF")["unreconciled_overruns"][0]["actual_cash_units"], 12)

    def test_halted_engine_only_allows_free_labels_other_engine_continues(self):
        self.forward()
        self.enqueue()
        task = self.loop.tick()
        with self.assertRaises(ControlError):
            self.ack(task, actual_time_seconds=11)
        self.loop.record_data("e1", self.other_h)
        self.enqueue("optional", input_hash=self.other_h, cash_units=0, time_seconds=0)
        self.enqueue("paid-label", kind="due_label", due_at=stamp(self.time), slot="forecast/6h")
        self.enqueue("free-label", kind="due_label", cash_units=0, time_seconds=0,
                     due_at=stamp(self.time), slot="forecast/24h")
        self.ready("g1", "GROWTH")
        self.enqueue("growth", eid="g1")
        self.assertEqual(self.loop.tick()["task_key"], "free-label")
        self.assertEqual(self.loop.tick()["task_key"], "growth")
        self.assertIsNone(self.loop.tick())
        self.assertEqual(self.loop.engine_status("GROWTH")["engine_halted"], 0)
        self.assertEqual(self.loop.engine_status("CF")["time_reserved"], 10)

    def test_restart_retains_claim_and_budget_without_automatic_retry(self):
        self.ready()
        self.enqueue()
        task = self.loop.tick()
        self.loop.close()
        self.loop = ResearchLoop(self.path, self.budgets, lambda: self.time)
        self.assertIsNone(self.loop.tick())
        self.assertEqual(self.loop.get_task("t1")["claim_token"], task["claim_token"])
        self.assertEqual(self.loop.engine_status("CF")["cash_reserved"], 10)
        self.ack(task)
        with self.assertRaises(ControlError):
            ResearchLoop(self.path, {"CF": {"cash_units": 200, "time_seconds": 100}})

    def test_two_connections_cannot_claim_same_task(self):
        self.ready()
        self.enqueue()
        other = ResearchLoop(self.path, clock=lambda: self.time)
        self.addCleanup(other.close)
        self.assertIsNotNone(self.loop.tick())
        self.assertIsNone(other.tick())

    def test_deadline_is_inconclusive_not_rejected_or_loss(self):
        self.forward()
        self.enqueue()
        self.time += timedelta(days=2)
        self.assertIsNone(self.loop.tick())
        self.assertEqual(self.loop.status("e1")["state"], "INCONCLUSIVE")
        self.assertFalse(self.loop.status("e1")["edge_proven"])
        self.assertEqual(self.loop.status("e1")["cash_spent"], 0)

    def test_rejected_late_write_cannot_rollback_deadline(self):
        self.forward()
        self.time += timedelta(days=2)
        with self.assertRaises(ControlError):
            self.loop.record_data("e1", self.other_h)
        self.assertEqual(self.loop.status("e1")["state"], "INCONCLUSIVE")

    def test_untrusted_pass_never_changes_state(self):
        self.forward()
        self.loop.record_evidence_verdict("e1", "pass", "UNTRUSTED_SYNTHETIC_FLAG")
        self.assertEqual(self.loop.status("e1")["state"], "FORWARD_TEST")

    def test_attested_pass_only_candidate_review_never_live(self):
        self.forward()
        self.loop.record_evidence_verdict("e1", "pass", "synthetic-audited-receipt", verified_receipt=True)
        result = self.loop.status("e1")
        self.assertEqual(result["display_state"], "SIMULATION_EVIDENCE_CANDIDATE_REVIEW_REQUIRED")
        self.assertFalse(result["edge_proven"])
        self.assertFalse(result["live_enabled"])
        self.loop.record_evidence_verdict("e1", "pass", "synthetic-audited-receipt", verified_receipt=True)
        with self.assertRaises(ControlError):
            self.loop.record_evidence_verdict("e1", "reject", "changed-mind", verified_receipt=True)

    def test_audit_and_spec_cannot_be_rewritten(self):
        self.ready()
        with self.assertRaises(sqlite3.IntegrityError):
            self.loop.db.execute("DELETE FROM audit")
        with self.assertRaises(sqlite3.IntegrityError):
            self.loop.db.execute("UPDATE experiments SET spec_hash='changed' WHERE id='e1'")

    def test_pause_resume_keeps_spec_and_alpha(self):
        self.forward()
        before = self.loop.status("e1")
        self.loop.suspend("e1", "PAUSED", "synthetic review")
        self.loop.resume("e1")
        after = self.loop.status("e1")
        self.assertEqual(after["state"], "FORWARD_TEST")
        self.assertEqual(before["spec_hash"], after["spec_hash"])
        self.assertEqual(before["alpha"], after["alpha"])

    def test_demo_is_explicitly_synthetic_and_untrusted(self):
        result = demo(Path(self.temp.name) / "demo.sqlite")
        self.assertEqual(result["selected_order"], ["demo-label", "demo-research"])
        self.assertEqual(result["status"]["state"], "FORWARD_TEST")
        self.assertEqual(result["status"]["spec"]["evidence_mode"], "SYNTHETIC")
        self.assertFalse(result["status"]["edge_proven"])


if __name__ == "__main__":
    unittest.main()
