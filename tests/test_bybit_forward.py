"""Synthetic source records only: no network, models, or exchange orders."""
import base64
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from one_trading.ai_native.bybit_forward import Capture, StopCapture, SYMBOLS, retry_delay, network
from one_trading.ai_native.engine import Frame

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def payload(symbol, seconds=0, u=1, **changes):
    obj = {"topic": "orderbook.1." + symbol, "type": "snapshot", "ts": int((BASE.timestamp()+seconds)*1000),
           "cts": int((BASE.timestamp()+seconds)*1000),
           "data": {"s": symbol, "b": [["100", "4"]], "a": [["101", "5"]], "u": u, "seq": 5}}
    obj.update(changes)
    return json.dumps(obj, indent=1).encode()


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "capture"
        self.c = Capture(self.path, BASE, 0, duration=20, max_bytes=20_000)
        self.c.reconnect(BASE, 0)

    def tearDown(self):
        self.c.close("TEST", BASE + timedelta(seconds=self.c.last_mono), self.c.last_mono)
        self.tmp.cleanup()

    def ingest(self, symbol, t=0, data=None):
        self.c.ingest(data or payload(symbol, t), BASE+timedelta(seconds=t), t)

    def sample(self, t):
        self.c.sample(BASE+timedelta(seconds=t), t)

    def rows(self, name):
        return [json.loads(x) for x in (self.path / (name + ".jsonl")).read_text().splitlines()]

    def test_received_bytes_hash_order_and_packet_provenance(self):
        original = payload(SYMBOLS[0])
        self.ingest(SYMBOLS[0], data=original)
        for s in SYMBOLS[1:]: self.ingest(s)
        self.sample(1)
        self.sample(1.5)
        raw = self.rows("raw")
        self.assertEqual([r["seq"] for r in raw], [1, 2, 3])
        self.assertEqual(base64.b64decode(raw[0]["payload_base64"]), original)
        self.assertEqual(raw[0]["sha256"], hashlib.sha256(original).hexdigest())
        frames = self.rows("frames")
        self.assertEqual(len(frames), 1)
        Frame.parse(frames[0])
        self.assertEqual(self.rows("events")[-1]["source_seqs"], dict(zip(SYMBOLS, (1, 2, 3))))

    def test_missing_stale_and_reconnect_discard(self):
        for s in SYMBOLS: self.ingest(s)
        self.sample(1)
        self.sample(5)
        self.assertEqual(len(self.rows("frames")), 1)
        self.c.reconnect(BASE+timedelta(seconds=5), 5)
        for s in SYMBOLS[:2]: self.ingest(s, 5)
        self.sample(6)
        self.assertEqual(len(self.rows("frames")), 1)
        self.ingest(SYMBOLS[2], 6)
        self.sample(7)
        self.assertEqual(len(self.rows("frames")), 2)

    def test_duplicate_u_is_fresh_keepalive(self):
        for s in SYMBOLS: self.ingest(s)
        for s in SYMBOLS: self.ingest(s, 3, payload(s, 3, u=1))
        self.sample(4)
        self.assertEqual(len(self.rows("frames")), 1)

    def test_future_exchange_and_bad_book_invalidate_previous(self):
        for s in SYMBOLS: self.ingest(s)
        self.ingest(SYMBOLS[0], 1, payload(SYMBOLS[0], 2))
        self.sample(2)
        self.assertEqual(self.rows("frames"), [])
        self.ingest(SYMBOLS[0], 2)
        self.ingest(SYMBOLS[1], 2, payload(SYMBOLS[1], 2, data={"s": SYMBOLS[1], "b": [], "a": [], "u": 1, "seq": 1}))
        self.sample(3)
        self.assertEqual(self.rows("frames"), [])

    def test_skipped_ticks_not_backfilled_and_clock_regression_stops(self):
        for s in SYMBOLS: self.ingest(s)
        self.sample(3)
        self.assertEqual(len(self.rows("frames")), 1)
        self.assertEqual(self.rows("events")[-2]["count"], 2)
        with self.assertRaisesRegex(StopCapture, "CLOCK_REGRESSION"):
            self.c.sample(BASE, 4)

    def test_source_sequence_and_timestamp_regression_rejected_and_logged(self):
        for s in SYMBOLS: self.ingest(s, 1)
        old_seq = json.loads(payload(SYMBOLS[0], 2))
        old_seq["data"]["seq"] = 4
        old_seq["data"]["u"] = 2
        self.ingest(SYMBOLS[0], 2, json.dumps(old_seq).encode())
        self.sample(2)
        self.assertFalse(self.rows("frames"))
        self.assertEqual([e for e in self.rows("events") if e["kind"] == "source_integrity_rejection"][-1]["reason"], "SOURCE_CLOCK_REGRESSION")
        self.ingest(SYMBOLS[0], 3)
        self.ingest(SYMBOLS[1], 3, payload(SYMBOLS[1], 0))
        self.sample(3)
        self.assertFalse(self.rows("frames"))
        self.assertEqual([e for e in self.rows("events") if e["kind"] == "source_integrity_rejection"][-1]["reason"], "SOURCE_CLOCK_REGRESSION")
        self.c.ingest(b"{bad", BASE+timedelta(seconds=3), 3)
        self.assertEqual(self.rows("events")[-1]["reason"], "INVALID_PAYLOAD")
        self.assertEqual(self.rows("events")[-1]["raw_seq"], len(self.rows("raw")))

    def test_source_restart_allows_sequence_reset_only_with_nonregressing_cts(self):
        for s in SYMBOLS: self.ingest(s, 1)
        restarting = json.loads(payload(SYMBOLS[0], 2, u=1))
        restarting["data"]["seq"] = 1
        self.ingest(SYMBOLS[0], 2, json.dumps(restarting).encode())
        self.sample(2)
        self.assertEqual(len(self.rows("frames")), 1)
        self.assertTrue(any(e["kind"] == "source_restart" for e in self.rows("events")))
        restarting["cts"] = int(BASE.timestamp()*1000)
        restarting["data"]["seq"] = 0
        self.ingest(SYMBOLS[0], 3, json.dumps(restarting).encode())
        self.sample(3)
        self.assertEqual(len(self.rows("frames")), 1)

    def test_quota_duration_naming_and_terminal(self):
        with self.assertRaises(FileExistsError):
            Capture(self.path, BASE, 0)
        with self.assertRaisesRegex(StopCapture, "DURATION"):
            self.c.sample(BASE + timedelta(seconds=20), 20)
        self.c.close("DURATION", BASE+timedelta(seconds=20), 20)
        self.assertEqual(self.rows("events")[-1]["reason"], "DURATION")
        self.assertLessEqual(sum(p.stat().st_size for p in self.path.iterdir()), 20_000)
        tiny = Capture(Path(self.tmp.name)/"tiny", BASE, 0, duration=10, max_bytes=5120)
        try:
            with self.assertRaisesRegex(StopCapture, "STORAGE_QUOTA"):
                for _ in range(100): tiny.ingest(payload(SYMBOLS[0]), BASE, 0)
            tiny.close("STORAGE_QUOTA", BASE, 0)
            self.assertLessEqual(sum(p.stat().st_size for p in (Path(self.tmp.name)/"tiny").iterdir()), 5120)
        finally:
            tiny.close("TEST", BASE, 0)

    def test_retry_backoff_is_bounded(self):
        self.assertEqual([retry_delay(i) for i in range(1, 8)], [1, 2, 4, 8, 16, 30, 30])

    def test_subscription_failure_is_retained_and_stops(self):
        with self.assertRaisesRegex(StopCapture, "SUBSCRIPTION_REJECTED"):
            self.c.ingest(b'{"op":"subscribe","success":false}', BASE, 0)
        self.assertEqual(self.rows("events")[-1]["kind"], "subscription_failure")
        self.assertEqual(len(self.rows("raw")), 1)

    def test_no_valid_book_halts(self):
        c = Capture(Path(self.tmp.name)/"no-book", BASE, 0, duration=400, max_bytes=10000)
        try:
            with self.assertRaisesRegex(StopCapture, "NO_VALID_BOOK"):
                c.sample(BASE + timedelta(seconds=300), 300)
        finally:
            c.close("NO_VALID_BOOK", BASE+timedelta(seconds=300), 300)

    def test_mock_network_subscription_rejection_stops(self):
        try:
            import websockets
        except ImportError:
            self.skipTest("optional websockets package absent")
        class FakeSocket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, value): self.subscription = json.loads(value)
            async def recv(self, decode=False): return b'{"op":"subscribe","success":false}'
        c = Capture(Path(self.tmp.name)/"mock-network", datetime.now(timezone.utc), time.monotonic(),
                    duration=10, max_bytes=10000)
        socket = FakeSocket()
        try:
            with patch.object(websockets, "connect", return_value=socket) as connect:
                self.assertEqual(asyncio.run(network(c)), "SUBSCRIPTION_REJECTED")
            self.assertEqual(socket.subscription["args"], ["orderbook.1."+s for s in SYMBOLS])
            self.assertEqual(connect.call_count, 1)
        finally:
            c.close("SUBSCRIPTION_REJECTED", datetime.now(timezone.utc), time.monotonic())

    def test_mock_network_non_upgrading_http200_fails_fast(self):
        try:
            import websockets
            from websockets.exceptions import WebSocketException
        except ImportError:
            self.skipTest("optional websockets package absent")
        class FakeHttp200(WebSocketException):
            response = type("Response", (), {"status_code": 200})()
        c = Capture(Path(self.tmp.name)/"http200-network", datetime.now(timezone.utc), time.monotonic(),
                    duration=10, max_bytes=10000)
        try:
            with patch.object(websockets, "connect", side_effect=FakeHttp200) as connect:
                self.assertEqual(asyncio.run(network(c)), "WS_UPGRADE_NOT_AVAILABLE")
            self.assertEqual(connect.call_count, 1)
            event = [e for e in [json.loads(line) for line in
                     (Path(self.tmp.name)/"http200-network"/"events.jsonl").read_text().splitlines()]
                     if e["kind"] == "connection_failure"][-1]
            self.assertEqual(event["http_status"], 200)
        finally:
            c.close("WS_UPGRADE_NOT_AVAILABLE", datetime.now(timezone.utc), time.monotonic())


if __name__ == "__main__": unittest.main()
