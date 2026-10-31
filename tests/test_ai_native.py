"""No network, model calls, runtime collection, or real exchange in these tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from one_trading.ai_native.__main__ import run
from one_trading.ai_native.audit import AuditError, digest, verify
from one_trading.ai_native.engine import Experiment, Plan, Protocol, ReplayError
from one_trading.ai_native.planners import CashPlanner, CodexPlanner, OnlineLearnerPlanner, plan


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def packet(index, price=100.0, *, age=0, gap=0.1, ask_depth=50_000,
           bid_depth=50_000, second_step=60):
    recv = BASE + timedelta(seconds=index * second_step)
    return dict(id=f"source-{index}", received_at=recv.isoformat(), quotes={
        "BTCUSDT": dict(bid=price, ask=price + gap, bid_size=bid_depth,
                        ask_size=ask_depth,
                        exchange_at=(recv - timedelta(seconds=age)).isoformat())})


class SequencePlanner:
    def __init__(self, actions):
        self.actions = actions
        self.inputs = []

    def propose(self, context):
        self.inputs.append(copy.deepcopy(context))
        action = self.actions.get(context["frame_id"], "HOLD")
        return plan(action, "BTCUSDT" if action != "HOLD" else "CASH",
                    0.10 if action == "BUY" else (1.0 if action == "SELL" else 0.0),
                    "Offline test price decision")


class MissingPlanner:
    def propose(self, context):
        return None


class AINativeTest(unittest.TestCase):
    def experiment(self, actions=None, **limits):
        planner = SequencePlanner(actions or {"source-0": "BUY", "source-2": "SELL"})
        parameters = dict(execution_delay_seconds=0, label_horizon_seconds=120)
        parameters.update(limits)
        engine = Experiment(Protocol(**parameters), {"CASH": CashPlanner(), "AI": planner},
                            data_use="SYNTHETIC")
        return engine, planner

    def test_buy_sell_delayed_costed_and_independently_recomputed(self):
        engine, planner = self.experiment()
        for i, price in enumerate((100, 102, 103, 104)):
            engine.observe(packet(i, price))
        result = engine.finish()
        audit = verify(result)
        self.assertEqual(audit["counts"]["fills"], 2)
        self.assertEqual(result["summary"]["CASH"]["fills"], 0)
        self.assertEqual(result["summary"]["AI"]["fills"], 2)
        self.assertEqual(result["summary"]["AI"]["open_positions"], 0)
        self.assertGreater(result["summary"]["AI"]["realized_pnl"], 0)
        events = result["ledger"]
        fills = [r["payload"] for r in events if r["kind"] == "paper_fill"]
        self.assertEqual([r["order"]["source_frame"] for r in fills], ["source-0", "source-2"])
        self.assertEqual([r["quote_frame"] for r in fills], ["source-1", "source-3"])
        self.assertFalse(planner.inputs[0]["resolved_memory"])
        self.assertFalse(planner.inputs[1]["resolved_memory"])
        self.assertTrue(planner.inputs[2]["resolved_memory"])
        self.assertIn("AI", audit["arms"])

    def test_execution_waits_for_next_receipt_after_latency(self):
        engine, _ = self.experiment({"source-0": "BUY"}, execution_delay_seconds=90)
        for i in range(4):
            engine.observe(packet(i))
        fills = [r["payload"] for r in engine.finish()["ledger"] if r["kind"] == "paper_fill"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["quote_frame"], "source-2")

    def test_stale_quote_rejects_paper_fill(self):
        engine, _ = self.experiment({"source-0": "BUY"})
        engine.observe(packet(0))
        engine.observe(packet(1, age=31))
        result = engine.finish()
        self.assertEqual(result["summary"]["AI"]["rejected_orders"], 1)
        self.assertEqual(result["summary"]["AI"]["fills"], 0)
        verify(result)

    def test_stale_due_price_does_not_become_training_memory(self):
        engine, planner = self.experiment({"source-0": "BUY"})
        for frame in (packet(0), packet(1), packet(2, age=40), packet(3)):
            engine.observe(frame)
        result = engine.finish()
        self.assertEqual([r["payload"]["source_frame"] for r in result["ledger"]
                          if r["kind"] == "matured_label" and r["payload"]["arm"] == "AI"],
                         ["source-3"])
        self.assertFalse(planner.inputs[2]["resolved_memory"])
        self.assertTrue(planner.inputs[3]["resolved_memory"])
        verify(result)

    def test_spread_and_depth_reject_invalid_execution(self):
        for kw in ({"gap": 1.0}, {"ask_depth": 1}):
            with self.subTest(kw=kw):
                engine, _ = self.experiment({"source-0": "BUY"})
                engine.observe(packet(0))
                engine.observe(packet(1, **kw))
                result = engine.finish()
                self.assertEqual(result["summary"]["AI"]["fills"], 0)
                self.assertEqual(result["summary"]["AI"]["rejected_orders"], 1)
                verify(result)

    def test_unavailable_ai_not_silently_substituted_with_cash(self):
        engine = Experiment(Protocol(), {"CASH": CashPlanner(), "AI": MissingPlanner()},
                            data_use="SYNTHETIC")
        for i in range(2):
            engine.observe(packet(i))
        result = engine.finish()
        self.assertEqual(result["summary"]["AI"]["model_unavailable"], 2)
        self.assertEqual(len([r for r in result["ledger"]
                              if r["kind"] == "decision" and r["payload"]["arm"] == "AI"]), 0)
        self.assertEqual(verify(result)["counts"]["unavailable"], 2)

    def test_time_identity_and_universe_fail_closed(self):
        engine, _ = self.experiment()
        engine.observe(packet(0))
        with self.assertRaises(ReplayError):
            engine.observe(packet(0))
        with self.assertRaises(ReplayError):
            engine.observe(packet(1, age=-1))
        other = packet(1)
        other["quotes"]["ETHUSDT"] = other["quotes"]["BTCUSDT"]
        with self.assertRaises(ReplayError):
            engine.observe(other)

    def test_bad_protocol_and_plan(self):
        with self.assertRaises(ReplayError):
            Protocol(execution_delay_seconds=-2)
        with self.assertRaises(ReplayError):
            Plan.parse(plan("BUY", "BTCUSDT", 5, "oversized"), {"BTCUSDT"})
        with self.assertRaises(ReplayError):
            Plan.parse(plan("SELL", "BADUSDT", 1, "wrong asset"), {"BTCUSDT"})

    def test_tampered_fill_rejected_even_with_rehashed_chain(self):
        engine, _ = self.experiment()
        for i in range(4):
            engine.observe(packet(i, 100 + i))
        result = engine.finish()
        forged = copy.deepcopy(result)
        fill = next(r for r in forged["ledger"] if r["kind"] == "paper_fill")
        fill["payload"]["execution_price"] *= 0.8
        tip = "0" * 64
        for row in forged["ledger"]:
            row["previous_hash"] = tip
            row["hash"] = digest({k: v for k, v in row.items() if k != "hash"})
            tip = row["hash"]
        forged["ledger_tip"] = tip
        with self.assertRaisesRegex(AuditError, "ACCOUNT_MISMATCH:execution_price"):
            verify(forged)

    def test_fabricated_rejection_cannot_hide_a_valid_losing_fill(self):
        engine, _ = self.experiment({"source-0": "BUY"})
        for i, price in enumerate((100, 101, 90)):
            engine.observe(packet(i, price))
        forged = copy.deepcopy(engine.finish())
        row = next(item for item in forged["ledger"] if item["kind"] == "paper_fill")
        prior = row["payload"]
        row["kind"] = "order_rejected"
        row["payload"] = dict(arm=prior["arm"], order=prior["order"],
                              receipt_frame=prior["quote_frame"], reason="SPREAD_LIMIT")
        tip = "0" * 64
        for item in forged["ledger"]:
            item["previous_hash"] = tip
            item["hash"] = digest({k: v for k, v in item.items() if k != "hash"})
            tip = item["hash"]
        forged["ledger_tip"] = tip
        with self.assertRaisesRegex(AuditError, "FALSE_OR_WRONG_REJECTION"):
            verify(forged)

    def test_tampered_mature_label_rejected_even_if_rehashed(self):
        engine, _ = self.experiment()
        for i in range(4):
            engine.observe(packet(i, 100 + i))
        result = engine.finish()
        forged = copy.deepcopy(result)
        label = next(r for r in forged["ledger"] if r["kind"] == "matured_label")
        label["payload"]["net_markout"] = 3.0
        tip = "0" * 64
        for row in forged["ledger"]:
            row["previous_hash"] = tip
            row["hash"] = digest({k: v for k, v in row.items() if k != "hash"})
            tip = row["hash"]
        forged["ledger_tip"] = tip
        with self.assertRaisesRegex(AuditError, "ACCOUNT_MISMATCH:mature_markout"):
            verify(forged)

    def test_codex_absent_never_claims_model_trade(self):
        with tempfile.TemporaryDirectory() as dirname:
            planner = CodexPlanner(runtime=Path(dirname), model="no-model", max_calls=1,
                                   executable="/definitely/not/a/codex")
            context = dict(frame_id="x", cutoff=BASE.isoformat(), history={},
                           universe=["BTCUSDT"], positions={}, pending_order=False)
            self.assertIsNone(planner.propose(context))
            self.assertEqual(planner.call_count, 0)

    def test_online_learner_changes_choice_using_only_matured_own_labels(self):
        learner = OnlineLearnerPlanner()
        context = {"history": {"BTCUSDT": {"previous_mid": 100.0, "current_mid": 101.0}},
                   "positions": {}, "resolved_memory": []}
        first = learner.propose(context)
        self.assertEqual(first["action"], "BUY")
        context["resolved_memory"] = [
            {"action": "BUY", "symbol": "BTCUSDT", "net_markout": -0.08}
            for _ in range(3)]
        revised = learner.propose(context)
        self.assertEqual(revised["action"], "HOLD")
        self.assertEqual(revised["symbol"], "CASH")

    def test_cli_synthetic_control_and_independent_audit(self):
        with tempfile.TemporaryDirectory() as dirname:
            source = Path(dirname) / "input.jsonl"
            source.write_text("\n".join(json.dumps(packet(i, 100 + i)) for i in range(5)))
            target = Path(dirname) / "result.json"
            opts = SimpleNamespace(input=str(source), output=str(target), data_use="SYNTHETIC",
                                   ai_mode="off", private_model_runtime=None,
                                   max_ai_calls=None, model="gpt-6-astra",
                                   model_executable="codex", model_timeout_seconds=60)
            response = run(opts)
            self.assertEqual(response["independent_audit"], "PASS")
            report = json.loads(target.read_text())
            self.assertNotIn("AI_FREE_DECISION", report["summary"])
            self.assertFalse(report["model_invocation_authorized"])
            verify(report)
            with self.assertRaises(ReplayError):
                run(opts)


if __name__ == "__main__":
    unittest.main()
