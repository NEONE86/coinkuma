"""Adversarial source/packet reconciliation independent of the capture writer."""
import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from one_trading.ai_native.bybit_audit import AuditError, audit
from one_trading.ai_native.bybit_forward import Capture, StopCapture, SYMBOLS

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
    path.write_bytes("".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                             for item in lines).encode())


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

    def test_monotonic_pre_sample_invalidation_rejected_even_if_wall_time_ties(self):
        folder = Path(self.temp.name) / "same-wall"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        bad = json.loads(message(SYMBOLS[0], 2))
        bad["data"]["seq"] = 0
        capture.ingest(json.dumps(bad).encode(), BASE + timedelta(seconds=3), 2)
        capture.sample(BASE + timedelta(seconds=3), 3)
        capture.close("TEST", BASE + timedelta(seconds=4), 4)
        def forge_frame(frames):
            second = json.loads(json.dumps(frames[0]))
            second["id"] = "bybit-00000002"
            second["received_at"] = (BASE + timedelta(seconds=3)).isoformat()
            frames.append(second)
        rewrite(folder / "frames.jsonl", forge_frame)
        def forge_event(events):
            events[-1]["frame_count"] = 2
            events.insert(-1, {"kind": "frame_provenance", "frame_id": "bybit-00000002",
                               "generation": 1, "sample_elapsed_seconds": 3,
                               "raw_cutoff_seq": 3,
                               "source_seqs": dict(zip(SYMBOLS, (1, 2, 3)))})
        rewrite(folder / "events.jsonl", forge_event)
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, "CUTOFF_OMITS_EARLIER_RECEIPT"):
            audit(folder)

    def test_post_sample_raw_wall_regression_rejected_with_later_monotonic(self):
        # Raw #4 has UTC t=2 but forged monotonic t=4; frame #2 is t=3 on
        # both clocks, so neither interpretation can be a valid append order.
        def forge_raw(raw):
            raw[3]["elapsed_seconds"] = 4
        rewrite(self.folder / "raw.jsonl", forge_raw)
        def forge_frame(frames):
            additional = json.loads(json.dumps(frames[0]))
            additional["id"] = "bybit-00000002"
            additional["received_at"] = (BASE + timedelta(seconds=3)).isoformat()
            frames.append(additional)
        rewrite(self.folder / "frames.jsonl", forge_frame)
        def forge_event(events):
            events[-1]["frame_count"] = 2
            events[-1]["at"] = (BASE + timedelta(seconds=5)).isoformat()
            events[-1]["elapsed_seconds"] = 5
            events.insert(-1, {"kind": "frame_provenance", "frame_id": "bybit-00000002",
                               "generation": 1, "sample_elapsed_seconds": 3,
                               "raw_cutoff_seq": 3,
                               "source_seqs": dict(zip(SYMBOLS, (1, 2, 3)))})
        rewrite(self.folder / "events.jsonl", forge_event)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "CUTOFF_OMITS_EARLIER_RECEIPT"):
            audit(self.folder)

    def test_forged_24h_completion_after_300s_book_gap_is_rejected(self):
        folder = Path(self.temp.name) / "forged-complete"
        capture = Capture(folder, BASE, 0, duration=86400, max_bytes=20000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        capture.close("NO_VALID_BOOK", BASE + timedelta(seconds=301), 301)
        def forge_terminal(events):
            events[-1]["reason"] = "DURATION"
            events[-1]["at"] = (BASE + timedelta(seconds=86400)).isoformat()
            events[-1]["elapsed_seconds"] = 86400
        rewrite(folder / "events.jsonl", forge_terminal)
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_DEADLINE_VIOLATION"):
            audit(folder)

    def test_equal_sample_and_later_receipt_clocks_are_not_backdated(self):
        folder = Path(self.temp.name) / "tie-after"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        # Network adapter records this after sampling even if one clock read
        # is reused for both operations; do not pretend it preceded the frame.
        capture.ingest(message(SYMBOLS[0], 1), BASE + timedelta(seconds=1), 1)
        capture.close("TEST", BASE + timedelta(seconds=2), 2)
        self.assertEqual(audit(folder)["frame_count"], 1)

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

    def test_cross_clock_reset_contradiction_rejected(self):
        folder = Path(self.temp.name) / "cross-clock"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        capture.reconnect(BASE + timedelta(seconds=4), 2)
        capture.sample(BASE + timedelta(seconds=4), 3)
        capture.close("TEST", BASE + timedelta(seconds=5), 5)
        def forge_frame(frames):
            additional = json.loads(json.dumps(frames[0]))
            additional["id"] = "bybit-00000002"
            # Monotonic t=3 after reset t=2, but UTC t=3 before reset t=4.
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
        with self.assertRaisesRegex(AuditError, "RESET_SAMPLE_CLOCK_CONTRADICTION"):
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
        with self.assertRaisesRegex(AuditError, "EVENT_TERMINAL_ORDER"):
            audit(self.folder)

    def test_genuine_subscription_rejection_has_consistent_terminal(self):
        folder = Path(self.temp.name) / "subscription-stopped"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        rejected = b'{"op":"subscribe","success":false,"ret_msg":"failed"}'
        with self.assertRaisesRegex(StopCapture, "SUBSCRIPTION_REJECTED"):
            capture.ingest(rejected, BASE + timedelta(seconds=2), 2)
        capture.close("SUBSCRIPTION_REJECTED", BASE + timedelta(seconds=3), 3)
        result = audit(folder)
        self.assertEqual(result["terminal_reason"], "SUBSCRIPTION_REJECTED")
        self.assertFalse(result["complete_24h"])

    def test_subscription_rejection_cannot_resume_with_fresh_books(self):
        folder = Path(self.temp.name) / "subscription-forged-continuation"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        with self.assertRaisesRegex(StopCapture, "SUBSCRIPTION_REJECTED"):
            capture.ingest(b'{"op":"subscribe","success":false}',
                           BASE + timedelta(seconds=2), 2)
        # Deliberately violate network()'s mandatory stop by appending another
        # valid frame from new receipts after the fatal subscription response.
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 3), BASE + timedelta(seconds=3), 3)
        capture.sample(BASE + timedelta(seconds=4), 4)
        capture.close("DURATION", BASE + timedelta(seconds=10), 10)
        with self.assertRaisesRegex(AuditError, "SUBSCRIPTION_CONTINUED_AFTER_REJECTION"):
            audit(folder)

    def test_subscription_rejection_cannot_be_relabelled_duration(self):
        folder = Path(self.temp.name) / "subscription-forged-terminal"
        capture = Capture(folder, BASE, 0, duration=10, max_bytes=20_000)
        capture.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            capture.ingest(message(symbol, 0), BASE, 0)
        capture.sample(BASE + timedelta(seconds=1), 1)
        with self.assertRaisesRegex(StopCapture, "SUBSCRIPTION_REJECTED"):
            capture.ingest(b'{"op":"subscribe","success":false}',
                           BASE + timedelta(seconds=2), 2)
        capture.close("DURATION", BASE + timedelta(seconds=10), 10)
        with self.assertRaisesRegex(AuditError, "SUBSCRIPTION_TERMINAL_MISMATCH"):
            audit(folder)


class ReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "review"
        self.capture = Capture(self.folder, BASE, 0, duration=600, max_bytes=100_000)
        self.capture.reconnect(BASE, 0)
        self.books(0, seq=100)
        self.capture.sample(BASE + timedelta(seconds=1), 1)

    def tearDown(self):
        self.capture.close("TEST", self.capture.last_wall, self.capture.last_mono)
        self.temp.cleanup()

    def books(self, second, seq=101, u=2, symbols=SYMBOLS):
        for symbol in symbols:
            obj = json.loads(message(symbol, second))
            obj["data"].update(seq=seq, u=u)
            self.capture.ingest(json.dumps(obj).encode(), BASE + timedelta(seconds=second), second)

    def close(self, reason="TEST", second=5):
        self.capture.close(reason, BASE + timedelta(seconds=second), second)

    def test_A_non_duration_cannot_resume_after_deadline(self):
        self.books(2)
        self.capture.sample(BASE + timedelta(seconds=3), 3)
        with self.assertRaises(StopCapture):
            self.capture.ingest(b'{"op":"subscribe","success":false}', BASE + timedelta(seconds=4), 4)
        self.close("SUBSCRIPTION_REJECTED", 5)
        self.assertEqual(audit(self.folder)["frame_count"], 2)
        # Move the second burst past the mandatory stop, recomputing original
        # payload hashes and exact file accounting, not just corrupting a hash.
        def shift_wall(value):
            at = datetime.fromisoformat(value)
            return (at + timedelta(seconds=398)).isoformat() if at >= BASE + timedelta(seconds=2) else value
        def shift_raw(records):
            for r in records:
                if r["elapsed_seconds"] >= 2:
                    r["elapsed_seconds"] += 398
                    r["received_at"] = shift_wall(r["received_at"])
                    obj = json.loads(base64.b64decode(r["payload_base64"]))
                    for key in ("ts", "cts"):
                        if key in obj: obj[key] += 398000
                    raw = json.dumps(obj).encode()
                    r.update(payload_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        rewrite(self.folder / "raw.jsonl", shift_raw)
        def shift_frames(records):
            for r in records:
                r["received_at"] = shift_wall(r["received_at"])
                for q in r["quotes"].values(): q["exchange_at"] = shift_wall(q["exchange_at"])
        rewrite(self.folder / "frames.jsonl", shift_frames)
        def shift_events(records):
            for r in records:
                if "at" in r: r["at"] = shift_wall(r["at"])
                for key in ("elapsed_seconds", "sample_elapsed_seconds"):
                    if r.get(key, 0) >= 2: r[key] += 398
        rewrite(self.folder / "events.jsonl", shift_events)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_DEADLINE"):
            audit(self.folder)

    def test_B_intermediate_restart_is_valid(self):
        self.books(2, seq=1, u=1)
        self.books(3, seq=2, u=2)
        self.capture.sample(BASE + timedelta(seconds=4), 4)
        self.close()
        self.assertEqual(audit(self.folder)["frame_count"], 2)

    def test_C_old_generation_cannot_cross_next_reset_monotonic(self):
        self.books(3)
        self.capture.reconnect(BASE + timedelta(seconds=4), 4)
        self.close()
        self.assertEqual(audit(self.folder)["frame_count"], 1)
        def move_reset(events):
            next(e for e in events if e.get("generation") == 2)["elapsed_seconds"] = 2
        rewrite(self.folder / "events.jsonl", move_reset)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "RAW_AFTER_CONNECTION"):
            audit(self.folder)

    def test_D_deleted_missed_ticks_and_counters_are_rejected(self):
        self.books(3)
        self.capture.sample(BASE + timedelta(seconds=4), 4)
        self.close()
        self.assertEqual(audit(self.folder)["missed_count"], 2)
        def remove_misses(events):
            events[:] = [e for e in events if e["kind"] != "missed_ticks"]
            events[-1].update(missed_count=0, unsampled_due_ticks=0)
        rewrite(self.folder / "events.jsonl", remove_misses)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SAMPLING|MISSED"):
            audit(self.folder)

    def test_A_exact_deadline_stops_raw_and_sample_but_normal_terminal_passes(self):
        for operation in (lambda: self.books(301),
                          lambda: self.capture.sample(BASE + timedelta(seconds=301), 301)):
            with self.assertRaisesRegex(StopCapture, "NO_VALID_BOOK"):
                operation()
        self.close("NO_VALID_BOOK", 301)
        result = audit(self.folder)
        self.assertEqual(result["raw_count"], 3)
        self.assertEqual(result["frame_count"], 1)

    def test_A_just_before_deadline_recovers(self):
        self.books(300.999)
        self.capture.sample(BASE + timedelta(seconds=300.999), 300.999)
        self.close("INTERRUPTED", 301)
        self.assertEqual(audit(self.folder)["frame_count"], 2)

    def test_A_first_frame_just_before_deadline_is_valid(self):
        folder = Path(self.temp.name) / "late-first"
        c = Capture(folder, BASE, 0, duration=600, max_bytes=100_000)
        c.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            c.ingest(message(symbol, 299), BASE + timedelta(seconds=299), 299)
        c.sample(BASE + timedelta(seconds=299.999), 299.999)
        c.close("INTERRUPTED", BASE + timedelta(seconds=300), 300)
        self.assertEqual(audit(folder)["frame_count"], 1)

    def test_A_normal_duration_before_book_deadline_and_equal_deadline(self):
        self.close("DURATION", 301)
        for duration in (300, 301):
            def limits(events):
                events[0]["duration_seconds"] = duration
                events[-1]["unsampled_due_ticks"] = duration - 2
            rewrite(self.folder / "events.jsonl", limits)
            reconcile_terminal(self.folder)
            self.assertEqual(audit(self.folder)["terminal_reason"], "DURATION")

    def test_A_premature_no_book_terminal_rejected(self):
        self.close("NO_VALID_BOOK", 299)
        with self.assertRaisesRegex(AuditError, "PREMATURE_NO_VALID_BOOK"):
            audit(self.folder)

    def test_A_earlier_duration_cannot_be_relabelled_no_book(self):
        self.close("NO_VALID_BOOK", 301)
        def shorten(events):
            events[0]["duration_seconds"] = 10
            events[-1]["unsampled_due_ticks"] = 8
        rewrite(self.folder / "events.jsonl", shorten)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_TERMINAL_PRIORITY"):
            audit(self.folder)

    def test_review_quota_stop_before_deadline_allows_later_cleanup(self):
        with self.assertRaisesRegex(StopCapture, "STORAGE_QUOTA"):
            self.capture.ingest(b'x' * 100_000, BASE+timedelta(seconds=300), 300)
        self.close("STORAGE_QUOTA", 302)
        self.assertEqual(audit(self.folder)["frame_count"], 1)

    def test_review_equal_deadline_requires_duration_reason(self):
        self.close("DURATION", 301)
        def shorten(events):
            events[0]["duration_seconds"] = 301
        rewrite(self.folder / "events.jsonl", shorten)
        reconcile_terminal(self.folder)
        self.assertEqual(audit(self.folder)["terminal_reason"], "DURATION")
        rewrite(self.folder / "events.jsonl", lambda e: e[-1].update(reason="NO_VALID_BOOK"))
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_TERMINAL_PRIORITY"):
            audit(self.folder)

    def quota_capture(self):
        with self.assertRaisesRegex(StopCapture, "STORAGE_QUOTA"):
            self.capture.ingest(b'x' * 100_000, BASE+timedelta(seconds=300), 300)
        self.close("STORAGE_QUOTA", 302)

    def test_review_quota_fatal_boundary_fields_cannot_be_forged(self):
        self.quota_capture()
        original = (self.folder / "events.jsonl").read_bytes()
        for field, value, error in (("reason", "TEST", "FATAL_STOP_REASON"),
                                    ("raw_count", 4, "FATAL_STOP_COUNTS"),
                                    ("sample_attempts", 2, "FATAL_STOP_COUNTS"),
                                    ("used_bytes", 0, "FATAL_STOP_QUOTA_EVIDENCE"),
                                    ("attempted_bytes", 1, "FATAL_STOP_QUOTA_EVIDENCE"),
                                    ("elapsed_seconds", 303, "FATAL_STOP_CHRONOLOGY"),
                                    ("at", (BASE+timedelta(seconds=303)).isoformat(), "FATAL_STOP_CHRONOLOGY")):
            with self.subTest(field=field):
                (self.folder / "events.jsonl").write_bytes(original)
                rewrite(self.folder / "events.jsonl", lambda e: e[-1]["fatal_stop"].update({field: value}))
                reconcile_terminal(self.folder)
                with self.assertRaisesRegex(AuditError, error):
                    audit(self.folder)

    def test_review_quota_fatal_boundary_cannot_precede_recorded_sample(self):
        self.quota_capture()
        def backdate(events):
            events[-1]["fatal_stop"].update(elapsed_seconds=0.5, at=(BASE+timedelta(seconds=.5)).isoformat())
        rewrite(self.folder / "events.jsonl", backdate)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "EVENT_TERMINAL_ORDER"):
            audit(self.folder)

    def test_review_quota_missing_stop_evidence_cannot_claim_late_cleanup(self):
        self.quota_capture()
        self.assertEqual(audit(self.folder)["collection_stop_elapsed_seconds"], 300)
        rewrite(self.folder / "events.jsonl", lambda e: e[-1].pop("fatal_stop"))
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_DEADLINE_TERMINAL"):
            audit(self.folder)

    def test_A_raw_after_deadline_without_another_frame_rejected(self):
        self.books(2)
        self.close("TEST", 302)
        def move_raw(records):
            for r in records[3:]:
                r["elapsed_seconds"] = 301
                r["received_at"] = (BASE + timedelta(seconds=301)).isoformat()
        rewrite(self.folder / "raw.jsonl", move_raw)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "NO_VALID_BOOK_DEADLINE_RAW"):
            audit(self.folder)

    def restart_capture(self):
        self.books(2, seq=1, u=1)
        self.books(3, seq=2, u=2)
        self.capture.sample(BASE + timedelta(seconds=4), 4)
        self.close()

    def test_B_restart_event_is_bound_to_raw_symbol_and_generation(self):
        self.restart_capture()
        self.assertEqual(audit(self.folder)["frame_count"], 2)
        original = (self.folder / "events.jsonl").read_bytes()
        for field, value in (("raw_seq", 7), ("symbol", "UNKNOWN"), ("generation", 2),
                             ("at", (BASE + timedelta(seconds=3)).isoformat())):
            with self.subTest(field=field):
                (self.folder / "events.jsonl").write_bytes(original)
                def forge(events):
                    next(e for e in events if e["kind"] == "source_restart")[field] = value
                rewrite(self.folder / "events.jsonl", forge)
                reconcile_terminal(self.folder)
                with self.assertRaisesRegex(AuditError, "SOURCE_RESTART_EVIDENCE_MISMATCH"):
                    audit(self.folder)

    def test_B_deleted_restart_event_rejected(self):
        self.restart_capture()
        rewrite(self.folder / "events.jsonl", lambda e: e.__setitem__(slice(None),
                [r for r in e if r["kind"] != "source_restart"]))
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SOURCE_RESTART_EVIDENCE_MISMATCH"):
            audit(self.folder)

    def test_B_restart_event_cannot_be_moved_after_its_frame(self):
        self.restart_capture()
        def move(events):
            restarts = [e for e in events if e["kind"] == "source_restart"]
            events[:] = [e for e in events if e["kind"] != "source_restart"]
            events[-1:-1] = restarts
        rewrite(self.folder / "events.jsonl", move)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SOURCE_RESTART_EVENT_ORDER"):
            audit(self.folder)

    def test_B_rehashed_regression_without_restart_and_regressed_cts_rejected(self):
        self.restart_capture()
        original_raw = (self.folder / "raw.jsonl").read_bytes()
        original_events = (self.folder / "events.jsonl").read_bytes()
        for mode in ("u", "cts"):
            with self.subTest(mode=mode):
                (self.folder / "raw.jsonl").write_bytes(original_raw)
                (self.folder / "events.jsonl").write_bytes(original_events)
                def forge(records):
                    for r in records[3:6]:
                        obj = json.loads(base64.b64decode(r["payload_base64"]))
                        if mode == "u": obj["data"]["u"] = 2
                        else: obj["cts"] = int(BASE.timestamp()*1000)-1
                        payload = json.dumps(obj).encode()
                        r.update(payload_base64=base64.b64encode(payload).decode(),
                                 sha256=hashlib.sha256(payload).hexdigest())
                rewrite(self.folder / "raw.jsonl", forge)
                rewrite(self.folder / "events.jsonl", lambda e: e.__setitem__(slice(None),
                        [r for r in e if r["kind"] != "source_restart"]))
                reconcile_terminal(self.folder)
                with self.assertRaisesRegex(AuditError, "FRAME_REFERENCES_INVALIDATED_SOURCE"):
                    audit(self.folder)

    def test_B_new_connection_resets_source_clocks_without_restart_event(self):
        self.capture.reconnect(BASE + timedelta(seconds=2), 2)
        self.books(2, seq=2, u=2)
        self.capture.sample(BASE + timedelta(seconds=3), 3)
        self.close()
        self.assertEqual(audit(self.folder)["frame_count"], 2)

    def test_C_old_generation_cannot_cross_next_reset_utc(self):
        self.books(3)
        self.capture.reconnect(BASE + timedelta(seconds=4), 4)
        self.close()
        def move_reset(events):
            next(e for e in events if e.get("generation") == 2)["at"] = (BASE + timedelta(seconds=2)).isoformat()
        rewrite(self.folder / "events.jsonl", move_reset)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "RAW_AFTER_CONNECTION"):
            audit(self.folder)

    def test_C_equal_clock_generation_boundary_is_valid(self):
        self.books(3)
        self.capture.reconnect(BASE + timedelta(seconds=3), 3)
        self.books(3, seq=2, u=2)
        self.capture.sample(BASE + timedelta(seconds=4), 4)
        self.close()
        self.assertEqual(audit(self.folder)["frame_count"], 2)

    def test_D_missed_frame_is_replayed_and_not_counted_as_skipped_tick(self):
        self.capture.sample(BASE + timedelta(seconds=5), 5)  # 3 skipped + stale attempt
        self.books(6)
        self.capture.sample(BASE + timedelta(seconds=6.25), 6.25)
        self.close(second=7.75)
        self.assertEqual(audit(self.folder)["missed_count"], 4)
        def forge(events):
            next(e for e in events if e["kind"] == "missed_frame")["symbols"] = [SYMBOLS[0]]
        rewrite(self.folder / "events.jsonl", forge)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SAMPLING_FALSE_MISSING_BOOK"):
            audit(self.folder)

    def test_D_deleted_missed_frame_and_adjusted_counters_rejected(self):
        self.capture.sample(BASE + timedelta(seconds=2), 2)  # valid
        self.capture.reconnect(BASE + timedelta(seconds=3), 3)
        self.capture.sample(BASE + timedelta(seconds=3), 3)  # missing all
        self.books(4)
        self.capture.sample(BASE + timedelta(seconds=4), 4)
        self.close()
        self.assertEqual(audit(self.folder)["missed_count"], 1)
        def forge(events):
            events[:] = [e for e in events if e["kind"] != "missed_frame"]
            events[-1]["missed_count"] = 0
            events[-1]["sample_attempts"] -= 1
        rewrite(self.folder / "events.jsonl", forge)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SAMPLING"):
            audit(self.folder)

    def test_D_terminal_unsampled_ticks_recalculated(self):
        self.close(second=4.25)
        self.assertEqual(audit(self.folder)["frame_count"], 1)
        rewrite(self.folder / "events.jsonl", lambda e: e[-1].update(unsampled_due_ticks=0))
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SAMPLING_UNSAMPLED_COUNT"):
            audit(self.folder)

    def test_D_legacy_missing_time_is_explicitly_unverifiable(self):
        self.capture.sample(BASE + timedelta(seconds=5), 5)
        self.close(second=6)
        self.assertEqual(audit(self.folder)["missed_count"], 4)
        def remove_clock(events):
            events[0].pop("sampling_evidence_version")
            next(e for e in events if e["kind"] == "missed_frame").pop("sample_elapsed_seconds")
        rewrite(self.folder / "events.jsonl", remove_clock)
        reconcile_terminal(self.folder)
        with self.assertRaisesRegex(AuditError, "SAMPLING_EVIDENCE_REQUIRED"):
            audit(self.folder)

    def test_D_missing_before_first_raw_reconstructs_empty_book(self):
        folder = Path(self.temp.name) / "initial-missing"
        c = Capture(folder, BASE, 0, duration=10, max_bytes=100_000)
        c.reconnect(BASE, 0)
        c.sample(BASE + timedelta(seconds=1), 1)
        for s in SYMBOLS: c.ingest(message(s, 2), BASE + timedelta(seconds=2), 2)
        c.sample(BASE + timedelta(seconds=2), 2)
        c.close("INTERRUPTED", BASE + timedelta(seconds=3), 3)
        self.assertEqual(audit(folder)["missed_count"], 1)


if __name__ == "__main__":
    unittest.main()
