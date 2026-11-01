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


def reconcile_terminal(folder: Path) -> None:
    def update(events):
        terminal = events[-1]
        terminal["bytes_before_terminal"] = (
            (folder / "raw.jsonl").stat().st_size +
            (folder / "frames.jsonl").stat().st_size +
            sum(len(json.dumps(e, sort_keys=True, separators=(",", ":")).encode()) + 1
                for e in events[:-1]))
    rewrite(folder / "events.jsonl", update)


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
            prov["raw_cutoff_seq"] = 4
        rewrite(self.folder / "events.jsonl", mutate)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "CUTOFF_INCLUDES_FUTURE_RECEIPT"):
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
        capture.reconnect(BASE, 0)
        capture.close("NO_VALID_BOOK", BASE + timedelta(seconds=3), 3)
        with self.assertRaisesRegex(AuditError, "NO_VALID_MARKET_EVIDENCE"):
            audit(empty)

    def test_later_invalidation_cannot_resurrect_old_quote(self):
        folder = Path(self.temp.name) / "invalidated"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        bad = json.loads(message(SYMBOLS[0], 2))
        bad["data"]["seq"] = 0
        capture.ingest(json.dumps(bad).encode(), BASE + timedelta(seconds=2), 2)
        capture.sample(BASE + timedelta(seconds=3), 3)  # correctly missing BTC
        capture.close("TEST", BASE + timedelta(seconds=4), 4)
        original_frame = json.loads((folder / "frames.jsonl").read_text().splitlines()[0])
        def forge_frame(frames):
            new = json.loads(json.dumps(original_frame))
            new["id"] = "bybit-00000002"
            new["received_at"] = (BASE + timedelta(seconds=3)).isoformat()
            frames.append(new)
        rewrite(folder / "frames.jsonl", forge_frame)
        def forge_event(events):
            events[-1]["frame_count"] = 2
            events.insert(-1, {"kind": "frame_provenance", "frame_id": "bybit-00000002",
                               "generation": 1, "sample_elapsed_seconds": 3,
                               "raw_cutoff_seq": 4,
                               "source_seqs": dict(zip(SYMBOLS, (1, 2, 3)))})
        rewrite(folder / "events.jsonl", forge_event)
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, "FRAME_REFERENCES_INVALIDATED_SOURCE"):
            audit(folder)

    def test_earlier_raw_update_cannot_be_hidden_by_lower_cutoff(self):
        def forge_frame(frames):
            second = json.loads(json.dumps(frames[0]))
            second["id"] = "bybit-00000002"
            second["received_at"] = (BASE + timedelta(seconds=3)).isoformat()
            frames.append(second)
        rewrite(self.folder / "frames.jsonl", forge_frame)
        def forge_event(events):
            events[-1]["frame_count"] = 2
            events[-1]["at"] = (BASE + timedelta(seconds=4)).isoformat()
            events[-1]["elapsed_seconds"] = 4
            events.insert(-1, {"kind": "frame_provenance", "frame_id": "bybit-00000002",
                               "generation": 1, "sample_elapsed_seconds": 3,
                               "raw_cutoff_seq": 3,
                               "source_seqs": dict(zip(SYMBOLS, (1, 2, 3)))})
        rewrite(self.folder / "events.jsonl", forge_event)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "CUTOFF_OMITS_EARLIER_RECEIPT"):
            audit(self.folder)

    def test_reconnect_without_new_receipts_invalidates_prior_generation(self):
        folder = Path(self.temp.name) / "reconnected"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        capture.reconnect(BASE + timedelta(seconds=2), 2)
        capture.sample(BASE + timedelta(seconds=3), 3)
        capture.close("TEST", BASE + timedelta(seconds=4), 4)
        def forge_frame(frames):
            additional = json.loads(json.dumps(frames[0]))
            additional["id"] = "bybit-00000002"
            additional["received_at"] = (BASE + timedelta(seconds=3)).isoformat()
            frames.append(additional)
        rewrite(folder / "frames.jsonl", forge_frame)
        def forge_event(events):
            events[-1]["frame_count"] = 2
            events.insert(-1, {"kind": "frame_provenance", "frame_id": "bybit-00000002",
                               "generation": 1, "sample_elapsed_seconds": 3,
                               "raw_cutoff_seq": 3,
                               "source_seqs": dict(zip(SYMBOLS, (1, 2, 3)))})
        rewrite(folder / "events.jsonl", forge_event)
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, "FRAME_RECONNECT_INVALIDATION"):
            audit(folder)

    def test_frame_after_declared_duration_fails(self):
        def shorten(events):
            events[0]["duration_seconds"] = 1
        rewrite(self.folder / "events.jsonl", shorten)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "FRAME_AFTER_DURATION"):
            audit(self.folder)

    def test_frame_after_terminal_fails(self):
        def shorten(events):
            events[-1]["at"] = (BASE + timedelta(milliseconds=500)).isoformat()
            events[-1]["elapsed_seconds"] = 0.5
        rewrite(self.folder / "events.jsonl", shorten)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "FRAME_AFTER_TERMINAL"):
            audit(self.folder)


if __name__ == "__main__":
    unittest.main()
