"""Adversarial source/packet reconciliation independent of the capture writer."""
import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from one_trading.ai_native.bybit_audit import AuditError, audit
from one_trading.ai_native.bybit_forward import Capture, SYMBOLS

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message(symbol: str, second: int) -> bytes:
    return json.dumps({
        "topic": f"orderbook.1.{symbol}", "type": "snapshot",
        "ts": int((BASE.timestamp() + second) * 1000),
        "cts": int((BASE.timestamp() + second) * 1000),
        "data": {"s": symbol, "b": [["100", "4"]], "a": [["101", "5"]],
                 "u": second + 1, "seq": second + 5},
    }).encode()


def rewrite(path: Path, transform) -> None:
    lines = [json.loads(row) for row in path.read_text().splitlines()]
    transform(lines)
    path.write_text("".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                            for item in lines))


class IndependentBybitAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "sample"
        capture = Capture(self.folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        capture.ingest(message(SYMBOLS[0], 2), BASE + timedelta(seconds=2), 2)
        capture.close("TEST", BASE + timedelta(seconds=3), 3)

    def tearDown(self):
        self.temp.cleanup()

    def test_capture_is_internally_consistent_not_economic_proof(self):
        result = audit(self.folder)
        self.assertEqual(result["status"], "PASS_SOURCE_CONSISTENCY_ONLY")
        self.assertEqual(result["frame_count"], 1)
        self.assertEqual(result["model_decisions"], 0)
        self.assertEqual(result["economic_alpha"], "NOT_TESTED")
        self.assertFalse(result["complete_24h"])

    def test_frame_price_changed_without_source_rejected(self):
        def replace(frames):
            frames[0]["quotes"][SYMBOLS[0]]["bid"] = 105.0
        rewrite(self.folder / "frames.jsonl", replace)
        with self.assertRaisesRegex(AuditError, "FRAME_RAW_QUOTE_MISMATCH"):
            audit(self.folder)

    def test_rehashed_source_mutation_rejected_on_semantics(self):
        def rehash(raw):
            decoded = base64.b64decode(raw[0]["payload_base64"])
            changed = decoded.replace(b'"100"', b'"099"', 1)
            self.assertNotEqual(changed, decoded)
            raw[0]["payload_base64"] = base64.b64encode(changed).decode()
            raw[0]["sha256"] = hashlib.sha256(changed).hexdigest()
        rewrite(self.folder / "raw.jsonl", rehash)
        with self.assertRaisesRegex(AuditError, "FRAME_RAW_QUOTE_MISMATCH"):
            audit(self.folder)

    def test_provenance_future_receipt_rejected(self):
        def mutate(events):
            prov = next(e for e in events if e["kind"] == "frame_provenance")
            prov["source_seqs"][SYMBOLS[0]] = 4
        rewrite(self.folder / "events.jsonl", mutate)
        with self.assertRaisesRegex(AuditError, "SOURCE_RECEIVED_AFTER_SAMPLE_OR_STALE"):
            audit(self.folder)

    def test_missing_terminal_is_incomplete(self):
        def truncate(events):
            events.pop()
        rewrite(self.folder / "events.jsonl", truncate)
        with self.assertRaisesRegex(AuditError, "TERMINAL_MISSING"):
            audit(self.folder)

    def test_well_formed_zero_data_capture_is_not_a_source_pass(self):
        empty = Path(self.temp.name) / "empty"
        capture = Capture(empty, BASE, 0, duration=10, max_bytes=20_000)
        capture.close("NO_VALID_BOOK", BASE + timedelta(seconds=3), 3)
        with self.assertRaisesRegex(AuditError, "NO_VALID_MARKET_EVIDENCE"):
            audit(empty)


if __name__ == "__main__":
    unittest.main()
