"""Offline synthetic tests for prospective, receipt-time-only PAPER scalping."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from one_trading.ai_native.scalp_flow import (
    ScalpEngine, ScalpError, Session, Settings, digest, encode,
    public_network, replay, timestamp, verify_journal,
)


START = int(datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp() * 1000)


def book(t, bid=100.00, bid_size=4_000, ask_size=2_000, seq=None, *, source_t=None):
    return {"topic": "orderbook.1.SOLUSDT", "type": "snapshot",
            "ts": START + t, "cts": START + (t if source_t is None else source_t),
            "data": {"s": "SOLUSDT", "b": [[str(bid), str(bid_size)]],
                     "a": [[str(round(bid + .01, 5)), str(ask_size)]],
                     "u": 1, "seq": t if seq is None else seq}}


def buys(t, side="Buy", count=20, units=100, start_id=0):
    return {"topic": "publicTrade.SOLUSDT", "ts": START + t,
            "data": [{"s": "SOLUSDT", "S": side, "p": "100", "v": str(units),
                      "T": START + t, "i": str(start_id + i)} for i in range(count)]}


def send(engine, t, payload):
    return engine.ingest(START + t, encode(payload))


def long_open(engine):
    send(engine, 0, book(0))
    send(engine, 3_900, buys(3_900))
    decision = send(engine, 4_000, book(4_000, 100.05))
    assert any(e["kind"] == "order_queued" and e["purpose"] == "ENTRY" for e in decision), decision
    fill = send(engine, 4_500, book(4_500, 100.07))
    assert any(e["kind"] == "paper_fill" and e["purpose"] == "ENTRY" for e in fill), fill
    return decision, fill


class ScalpTests(unittest.TestCase):
    def test_flow_is_observed_then_filled_on_future_receipt(self):
        e = ScalpEngine(Settings())
        decision, fill = long_open(e)
        self.assertEqual(e.signals, 1)
        self.assertEqual(e.entry_fills, 1)
        self.assertEqual(e.position.side, 1)
        self.assertLessEqual(e.position.entry_notional, 3_000.00001)
        self.assertLessEqual(e.position.entry_notional, 100.08 * 2_000 * 0.03)
        self.assertEqual(decision[0]["kind"], "observation")
        self.assertEqual(fill[0]["kind"], "paper_fill")
        self.assertEqual(e.position.entered_ms, START + 4_500)

    def test_no_same_packet_fill_or_backdated_action(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        send(e, 3_900, buys(3_900))
        send(e, 4_000, book(4_000, 100.05))
        self.assertIsNotNone(e.pending)
        self.assertFalse(any(x["kind"] == "paper_fill" for x in send(e, 4_000, book(4_000, 100.05))))
        self.assertIsNone(e.position)
        with self.assertRaisesRegex(ScalpError, "RECEIPT_CLOCK_REGRESSION"):
            send(e, 3_999, book(3_999))

    def test_take_profit_requires_later_execution_quote_and_includes_costs(self):
        e = ScalpEngine(Settings())
        long_open(e)
        decision = send(e, 6_000, book(6_000, 101.0))
        self.assertIsNotNone(e.position)
        self.assertTrue(any(x["kind"] == "order_queued" and x["purpose"] == "EXIT" for x in decision))
        fill = send(e, 6_500, book(6_500, 101.05))
        self.assertIsNone(e.position)
        self.assertEqual(e.trades_closed, 1)
        self.assertGreater(e.cash, 10_000)
        self.assertTrue(any(x["kind"] == "trade_closed" for x in fill))
        self.assertLess(fill[0]["price"], 101.05)

    def test_stop_gap_partial_exit_keeps_unfilled_exposure(self):
        e = ScalpEngine(Settings())
        long_open(e)
        decision = send(e, 6_000, book(6_000, 99.60, bid_size=10))
        self.assertTrue(any(x["kind"] == "order_queued" and x["reason"] == "STOP" for x in decision))
        partial = send(e, 6_500, book(6_500, 99.45, bid_size=30))
        self.assertTrue(any(x["kind"] == "paper_fill" for x in partial))
        self.assertIsNotNone(e.position)
        self.assertGreater(e.position.quantity, 0)
        self.assertEqual(e.pending.purpose, "EXIT")
        self.assertLess(e.position.quantity, 30)
        send(e, 7_000, book(7_000, 99.40, bid_size=3_000))
        self.assertIsNone(e.position)
        self.assertLess(e.cash, 10_000)

    def test_insufficient_tape_and_opposite_book_prevent_signals(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        send(e, 3_900, buys(3_900, count=5, units=10))
        send(e, 4_000, book(4_000, 100.05))
        self.assertEqual(e.signals, 0)
        send(e, 5_000, buys(5_000, start_id=50))
        send(e, 6_000, book(6_000, 100.07, bid_size=100, ask_size=4_000))
        self.assertEqual(e.signals, 0)
        self.assertIsNone(e.position)

    def test_duplicate_trade_ids_do_not_inflate_pressure(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        payload = buys(3_900)
        send(e, 3_900, payload)
        send(e, 3_900, payload)
        self.assertEqual(len(e.trades), 20)

    def test_stale_future_and_regressing_book_invalidates_entry(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        send(e, 3_900, buys(3_900))
        send(e, 4_000, book(4_000, 100.05))
        bad = send(e, 4_500, book(4_500, 100.07, source_t=10_000))
        self.assertTrue(any(x["kind"] == "input_rejected" for x in bad))
        self.assertIsNone(e.position)
        self.assertIsNone(e.book)
        self.assertEqual(e.entry_fills, 0)

    def test_no_fill_if_visible_depth_insufficient(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        send(e, 3_900, buys(3_900))
        send(e, 4_000, book(4_000, 100.05))
        results = send(e, 4_500, book(4_500, 100.07, ask_size=3))
        self.assertEqual(results[0]["kind"], "no_fill")
        self.assertEqual(results[0]["reason"], "INSUFFICIENT_BOOK_CAPACITY")
        self.assertIsNone(e.position)

    def test_bounded_settings_reject_excess_leverage_or_risk(self):
        for overrides in ({"leverage": 10}, {"risk_fraction": 0.03}, {"target_bps": 5}):
            with self.assertRaises(ScalpError):
                replace(Settings(), **overrides)

    def test_short_position_follows_real_selling_pressure(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        send(e, 3_900, buys(3_900, side="Sell"))
        out = send(e, 4_000, book(4_000, bid=99.94, bid_size=2_000, ask_size=4_000))
        self.assertTrue(any(x["kind"] == "order_queued" and x["side"] == -1 for x in out))
        send(e, 4_500, book(4_500, bid=99.92, bid_size=2_000, ask_size=4_000))
        self.assertEqual(e.position.side, -1)
        out = send(e, 6_000, book(6_000, bid=98.95))
        self.assertTrue(any(x["kind"] == "order_queued" and x["purpose"] == "EXIT" for x in out))
        send(e, 6_500, book(6_500, bid=98.90))
        self.assertEqual(e.trades_closed, 1)
        self.assertGreater(e.cash, 10_000)

    def test_daily_risk_halt_enforces_no_new_entries(self):
        e = ScalpEngine(Settings())
        long_open(e)
        out = send(e, 6_000, book(6_000, bid=92))
        self.assertTrue(e.halted)
        self.assertTrue(any(x["kind"] == "risk_halt" for x in out))
        self.assertEqual(e.pending.purpose, "EXIT")
        send(e, 6_500, book(6_500, bid=91.9))
        self.assertIsNone(e.position)
        self.assertLess(e.cash, 10_000)
        send(e, 20_000, buys(20_000, start_id=100))
        send(e, 21_000, book(21_000, 101.0))
        self.assertEqual(e.signals, 1)

    def test_old_trade_messages_cannot_create_lookahead_edge(self):
        e = ScalpEngine(Settings())
        send(e, 0, book(0))
        old = buys(3_900)
        for row in old["data"]:
            row["T"] = START - 6_000
        rejected = send(e, 3_900, old)
        self.assertTrue(any(x["kind"] == "input_rejected" for x in rejected))
        send(e, 4_000, book(4_000, bid=100.05))
        self.assertEqual(e.signals, 0)

    def test_journal_hash_and_independent_decision_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            session = Session(path / "paper", Settings(), "OFFLINE_REPLAY")
            for t, payload in [(0, book(0)), (3_900, buys(3_900)),
                               (4_000, book(4_000, 100.05)),
                               (4_500, book(4_500, 100.07)),
                               (6_000, book(6_000, 101.0)),
                               (6_500, book(6_500, 101.05))]:
                session.ingest(START + t, encode(payload))
            session.close("REPLAY_COMPLETE")
            audit = verify_journal(path / "paper" / "events.jsonl")
            self.assertEqual(audit["source_and_decision_replay"], "IDENTICAL")
            self.assertEqual(audit["trades_closed"], 1)
            events = (path / "paper" / "events.jsonl").read_text().splitlines()
            altered = json.loads(events[4]); altered["payload"] = {"lie": True}
            events[4] = json.dumps(altered)
            (path / "paper" / "events.jsonl").write_text("\n".join(events) + "\n")
            with self.assertRaisesRegex(ScalpError, "JOURNAL_HASH_CHAIN_INVALID"):
                verify_journal(path / "paper" / "events.jsonl")

    def test_rehashing_fake_decision_still_fails_semantic_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper"
            session = Session(path, Settings(), "OFFLINE_REPLAY")
            session.ingest(START, encode(book(0)))
            session.close("REPLAY_COMPLETE")
            events = [json.loads(row) for row in (path / "events.jsonl").read_text().splitlines()]
            events.insert(-1, {"kind": "paper_fill", "payload": {"fake": True}})
            prev = "0" * 64
            for seq, row in enumerate(events):
                row.update(seq=seq, previous_hash=prev)
                row["hash"] = digest(encode({k: row[k] for k in ("seq", "previous_hash", "kind", "payload")}))
                prev = row["hash"]
            (path / "events.jsonl").write_bytes(b"\n".join(encode(r) for r in events) + b"\n")
            with self.assertRaisesRegex(ScalpError, "UNEXPECTED_DERIVED_EVENT"):
                verify_journal(path / "events.jsonl")

    def test_file_replay_and_verification_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            packets = [(0, book(0)), (3_900, buys(3_900)),
                       (4_000, book(4_000, 100.05)),
                       (4_500, book(4_500, 100.07)),
                       (6_000, book(6_000, 101.0)),
                       (6_500, book(6_500, 101.05))]
            feed = path / "feed.jsonl"
            feed.write_bytes(b"\n".join(encode({"received_at": timestamp(START + t),
                                                "payload": obj}) for t, obj in packets) + b"\n")
            session = Session(path / "run", Settings(), "OFFLINE_REPLAY")
            summary = replay(feed, session)
            self.assertEqual(summary["reason"], "REPLAY_COMPLETE")
            self.assertEqual(verify_journal(path / "run" / "events.jsonl")["trades_closed"], 1)

    def test_mock_public_stream_subscribes_then_records_replayable_feed(self):
        try:
            import websockets
        except ImportError:
            self.skipTest("optional websockets not installed")

        class FakeSocket:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, message):
                self.subscription = json.loads(message)

            async def recv(self, decode=False):
                self.calls += 1
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                return encode({"topic": "orderbook.1.SOLUSDT", "type": "snapshot",
                               "cts": now_ms, "ts": now_ms,
                               "data": {"s": "SOLUSDT", "b": [["100", "5000"]],
                                        "a": [["100.01", "2000"]], "u": 1, "seq": self.calls}})

        with tempfile.TemporaryDirectory() as tmp:
            socket = FakeSocket()
            s = Session(Path(tmp) / "live", Settings(), "PUBLIC_LINEAR_LIVE")
            with patch.object(websockets, "connect", return_value=socket):
                summary = asyncio.run(public_network(s, duration=3, max_inputs=6))
            self.assertEqual(summary["reason"], "MAX_INPUTS")
            self.assertEqual(socket.subscription["args"],
                             ["orderbook.1.SOLUSDT", "publicTrade.SOLUSDT"])
            self.assertEqual(verify_journal(Path(tmp) / "live" / "events.jsonl")["inputs"], 6)

    def test_subscription_failure_still_closes_a_verifiable_journal(self):
        try:
            import websockets
        except ImportError:
            self.skipTest("optional websockets not installed")

        class FakeSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, message):
                pass

            async def recv(self, decode=False):
                return b'{"op":"subscribe","success":false}'

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "failed"
            session = Session(path, Settings(), "PUBLIC_LINEAR_LIVE")
            with patch.object(websockets, "connect", return_value=FakeSocket()):
                with self.assertRaisesRegex(ScalpError, "SUBSCRIPTION_REJECTED"):
                    asyncio.run(public_network(session, duration=1, max_inputs=20))
            result = verify_journal(path / "events.jsonl")
            self.assertEqual(result["inputs"], 1)
            self.assertEqual(result["rejected_messages"], 1)


if __name__ == "__main__":
    unittest.main()
