"""Offline prospective evidence controls: no network, model client or orders."""

import base64
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from one_trading.ai_native.engine import ReplayError, canonical
from one_trading.ai_native.prospective_evidence import EvidenceRecorder
from one_trading.ai_native.prospective_audit import AuditError, digest, verify
from one_trading.ai_native.prospective_paper import replay


BASE = datetime(2026, 11, 4, tzinfo=timezone.utc)
COMMIT = "a" * 64
PROMPT = "b" * 64


def registry():
    return {
        "CASH": {"version": "cash-v1", "model": None, "prompt_sha256": None,
                 "call_limit": 0, "cost_limit_usdt": "0"},
        "PRICE_ONLY": {"version": "momentum-v1", "model": None, "prompt_sha256": None,
                       "call_limit": 0, "cost_limit_usdt": "0"},
        "AI_ECON": {"version": "economic-v1", "model": "mock-model", "prompt_sha256": PROMPT,
                    "call_limit": 2, "cost_limit_usdt": "0.10"},
        "AI_MEMORY": {"version": "memory-v1", "model": "mock-model", "prompt_sha256": PROMPT,
                      "call_limit": 2, "cost_limit_usdt": "0.10"},
    }


def frame(i):
    t = BASE + timedelta(seconds=i * 5)
    return {"id": f"frame-{i}", "received_at": t.isoformat(), "quotes": {
        "BTCUSDT": {"bid": 100.0+i, "ask": 100.1+i, "bid_size": 10000.0,
                    "ask_size": 10000.0, "exchange_at": t.isoformat()}}}


def refs(i, *, future=False):
    return {"BTCUSDT": {"seq": i+1, "sha256": "c" * 64,
            "received_at": (BASE + timedelta(seconds=i*5+(1 if future else 0))).isoformat()}}


def plan(action="BUY"):
    return {"action": action, "symbol": "BTCUSDT" if action != "HOLD" else "CASH",
            "fraction": 0.10 if action == "BUY" else 0.0, "p_up": 0.6,
            "thesis": "Test thesis", "counter_hypothesis": "Could reverse",
            "invalidation": "New quote"}


def rehash(rows):
    prior = "0" * 64
    for i, row in enumerate(rows):
        row["seq"] = i
        row["previous_hash"] = prior
        row["hash"] = digest({k: v for k, v in row.items() if k != "hash"})
        prior = row["hash"]
    return b"".join(canonical(row) + b"\n" for row in rows)


class ProspectiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "evidence.jsonl"

    def writer(self, **changes):
        options = dict(run_id="synthetic-001", source_class="SYNTHETIC", collector_commit=COMMIT,
                       registry=registry(), start_at=BASE, model_calls_authorized=True)
        options.update(changes)
        return EvidenceRecorder(self.path, **options)

    def freeze(self, writer, i=0, *, at_offset=0, memory=None):
        f = frame(i)
        writer.freeze(f, source_receipts=refs(i), raw_cutoff_seq=i+1,
                      memory=[] if memory is None else memory,
                      at=BASE + timedelta(seconds=i*5+at_offset), elapsed=i*5+at_offset)

    def request(self, writer, *, rid="request-1", policy="AI_ECON", i=0,
                seconds=1, reserve="0.05", deadline=3):
        writer.request(request_id=rid, frame_id=f"frame-{i}", policy_id=policy,
                       reserve_cost_usdt=reserve, deadline_seconds=deadline,
                       at=BASE+timedelta(seconds=seconds), elapsed=seconds)

    def normal(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=2), elapsed=2,
                        actual_cost_usdt="0.0234", response_bytes=canonical(plan()),
                        input_tokens=120, output_tokens=50)
        writer.decide("request-1", at=BASE+timedelta(seconds=2.1), elapsed=2.1)
        self.freeze(writer, 1)
        writer.close(at=BASE+timedelta(seconds=7), elapsed=7)
        return verify(self.path)

    def mutate(self, change):
        rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        change(rows)
        self.path.write_bytes(rehash(rows))

    def test_success_and_costed_timing(self):
        result = self.normal()
        self.assertEqual(result["status"], "PASS_SYNTHETIC_TIMING_ONLY")
        self.assertEqual(result["counts"]["validated"], 1)
        self.assertEqual(result["model_cost_usdt"]["AI_ECON"], "0.0234")
        self.assertEqual(result["paper_fills"], "NOT_TESTED")

    def test_reuses_existing_paper_engine_later_quote_only(self):
        self.normal()
        result = replay(self.path)
        self.assertEqual(result["status"], "PASS_OFFLINE_REPLAY_ONLY")
        self.assertEqual(result["summary"]["AI_ECON"]["fills"], 1)
        self.assertEqual(result["summary"]["CASH"]["fills"], 0)
        self.assertEqual(result["summary"]["AI_ECON"]["model_spend_usdt"], "0.0234")
        self.assertAlmostEqual(result["summary"]["AI_ECON"]["cost_adjusted_pnl_usdt"],
                               result["summary"]["AI_ECON"]["paper_pnl_usdt"]-0.0234)

    def test_failed_call_is_charged_and_no_duplicate(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=3), elapsed=3,
                        actual_cost_usdt="0.012", failure_reason="TIMEOUT")
        with self.assertRaises(ReplayError):
            self.request(writer, rid="request-1", seconds=4)
        writer.close(at=BASE+timedelta(seconds=4), elapsed=4)
        outcome = verify(self.path)
        self.assertEqual(outcome["counts"]["failed"], 1)
        self.assertEqual(outcome["model_cost_usdt"]["AI_ECON"], "0.012")

    def test_late_response_rejected_not_silently_accepted(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=5), elapsed=5,
                        actual_cost_usdt="0.01", response_bytes=canonical(plan()))
        writer.decide("request-1", at=BASE+timedelta(seconds=5), elapsed=5)
        writer.close(at=BASE+timedelta(seconds=6), elapsed=6)
        self.assertEqual(verify(self.path)["counts"]["rejected"], 1)

    def test_slow_proposal_validation_cannot_backdate_paper_eligibility(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=2), elapsed=2,
                        actual_cost_usdt="0.01", response_bytes=canonical(plan()))
        writer.decide("request-1", at=BASE+timedelta(seconds=4), elapsed=4)
        self.freeze(writer, 1)
        writer.close(at=BASE+timedelta(seconds=6), elapsed=6)
        self.assertEqual(verify(self.path)["counts"]["validated"], 1)
        # Decision validated at second 4, plus two-second delay; second-5 quote
        # cannot fill even though the provider answered at second 2.
        self.assertEqual(replay(self.path)["summary"]["AI_ECON"]["fills"], 0)

    def test_expired_proposal_rejected(self):
        writer = self.writer(proposal_ttl_seconds=1)
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=2), elapsed=2,
                        actual_cost_usdt="0.01", response_bytes=canonical(plan()))
        writer.decide("request-1", at=BASE+timedelta(seconds=4), elapsed=4)
        writer.close(at=BASE+timedelta(seconds=5), elapsed=5)
        self.assertEqual(verify(self.path)["counts"]["rejected"], 1)

    def test_real_cost_over_budget_fails_closed(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=2), elapsed=2,
                        actual_cost_usdt="0.20", response_bytes=canonical(plan()))
        writer.decide("request-1", at=BASE+timedelta(seconds=2), elapsed=2)
        writer.close(at=BASE+timedelta(seconds=3), elapsed=3)
        with self.assertRaisesRegex(AuditError, "ACTUAL_MODEL_COST_OVER_BUDGET"):
            verify(self.path)

    def test_future_source_and_immature_memory_refused_before_write(self):
        writer = self.writer()
        with self.assertRaisesRegex(ReplayError, "FUTURE_SOURCE_REF"):
            writer.freeze(frame(0), source_receipts=refs(0, future=True), raw_cutoff_seq=1,
                          memory=[], at=BASE, elapsed=0)
        with self.assertRaisesRegex(ReplayError, "IMMATURE_MEMORY"):
            self.freeze(writer, memory=[{"id":"outcome-1",
                "matured_at":(BASE+timedelta(seconds=1)).isoformat()}])
        writer.close(at=BASE+timedelta(seconds=1), elapsed=1)
        self.assertEqual(verify(self.path)["counts"]["frames"], 0)

    def test_unauthorized_live_model_requests_rejected(self):
        writer = self.writer(model_calls_authorized=False)
        self.freeze(writer)
        with self.assertRaisesRegex(ReplayError, "UNAUTHORIZED_MODEL_REQUEST"):
            self.request(writer)
        writer.close(at=BASE+timedelta(seconds=1), elapsed=1)
        verify(self.path)

    def test_stale_request_cannot_use_aged_market_input(self):
        writer = self.writer()
        self.freeze(writer)
        with self.assertRaisesRegex(ReplayError, "STALE_OR_FUTURE_REQUEST_INPUT"):
            self.request(writer, seconds=5.01)
        writer.close(at=BASE+timedelta(seconds=6), elapsed=6)
        self.assertEqual(verify(self.path)["counts"]["requests"], 0)

    def test_rehashed_stale_request_rejected_independently(self):
        self.normal()
        def forge(rows):
            rows[2]["at"] = (BASE+timedelta(seconds=5.1)).isoformat()
            rows[2]["elapsed_seconds"] = 5.1
            # Move the post-request events forward to avoid a trivial clock failure.
            rows[3]["at"] = (BASE+timedelta(seconds=5.2)).isoformat()
            rows[3]["elapsed_seconds"] = 5.2
            rows[4]["at"] = (BASE+timedelta(seconds=5.3)).isoformat()
            rows[4]["elapsed_seconds"] = 5.3
            rows[4]["payload"]["eligible_after"] = (BASE+timedelta(seconds=7.3)).isoformat()
            rows[5]["at"] = (BASE+timedelta(seconds=6)).isoformat()
            rows[5]["elapsed_seconds"] = 6
            rows[6]["at"] = (BASE+timedelta(seconds=7)).isoformat()
            rows[6]["elapsed_seconds"] = 7
            rows[2]["payload"]["deadline_at"] = (BASE+timedelta(seconds=8.1)).isoformat()
        self.mutate(forge)
        with self.assertRaisesRegex(AuditError, "STALE_REQUEST_INPUT"):
            verify(self.path)

    def test_live_source_requires_independent_source_directory(self):
        writer = self.writer(source_class="LIVE_PUBLIC")
        self.freeze(writer)
        writer.close(at=BASE+timedelta(seconds=1), elapsed=1)
        with self.assertRaisesRegex(AuditError, "LIVE_SOURCE_FILES_REQUIRED"):
            verify(self.path)

    def test_live_refs_bound_to_independently_audited_originals(self):
        writer = self.writer(source_class="LIVE_PUBLIC")
        self.freeze(writer)
        writer.close(at=BASE+timedelta(seconds=1), elapsed=1)
        folder = Path(self.tmp.name)/"source"
        folder.mkdir()
        (folder/"raw.jsonl").write_bytes(canonical(dict(seq=1, sha256="c"*64,
                                                received_at=BASE.isoformat()))+b"\n")
        (folder/"frames.jsonl").write_bytes(canonical(frame(0))+b"\n")
        (folder/"events.jsonl").write_bytes(canonical(dict(kind="frame_provenance",
            frame_id="frame-0", raw_cutoff_seq=1, source_seqs={"BTCUSDT":1}))+b"\n")
        # This unit test isolates the binding. The real independent source
        # auditor has separate full regression and tamper tests.
        with patch("one_trading.ai_native.bybit_audit.audit",
                   return_value={"status":"PASS_SOURCE_CONSISTENCY_ONLY"}):
            self.assertEqual(verify(self.path, source_dir=folder)["status"],
                             "PASS_BOUND_SOURCE_TIMING_ONLY")
            (folder/"events.jsonl").write_bytes(canonical(dict(kind="frame_provenance",
                frame_id="frame-0", raw_cutoff_seq=2, source_seqs={"BTCUSDT":1}))+b"\n")
            with self.assertRaisesRegex(AuditError, "SOURCE_PROVENANCE_MISMATCH"):
                verify(self.path, source_dir=folder)

    def test_writer_rejects_duplicate_output_and_keeps_bytes(self):
        writer = self.writer()
        writer.close(at=BASE, elapsed=0)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.writer()
        self.assertEqual(self.path.read_bytes(), before)

    def test_rehashed_future_source_attack_rejected(self):
        self.normal()
        def forge(rows):
            rows[1]["payload"]["source_receipts"]["BTCUSDT"]["received_at"] = (
                BASE+timedelta(seconds=2)).isoformat()
        self.mutate(forge)
        with self.assertRaisesRegex(AuditError, "FUTURE_OR_UNHASHED_SOURCE"):
            verify(self.path)

    def test_rehashed_response_backdating_rejected(self):
        self.normal()
        self.mutate(lambda rows: rows[3].update(at=(BASE+timedelta(seconds=0.5)).isoformat()))
        with self.assertRaises(AuditError):
            verify(self.path)

    def test_rehashed_late_proposal_approval_rejected(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=5), elapsed=5,
                        actual_cost_usdt="0.01", response_bytes=canonical(plan()))
        writer.decide("request-1", at=BASE+timedelta(seconds=5), elapsed=5)
        writer.close(at=BASE+timedelta(seconds=6), elapsed=6)
        self.mutate(lambda rows: rows[4].update(kind="proposal_validated", payload=dict(
            request_id="request-1", plan=plan(), source_frame="frame-0",
            response_sha256=rows[3]["payload"]["response_sha256"],
            eligible_after=(BASE+timedelta(seconds=7)).isoformat(),
            valid_until=(BASE+timedelta(seconds=125)).isoformat())))
        with self.assertRaisesRegex(AuditError, "INVALID_OR_LATE_PLAN_APPROVED"):
            verify(self.path)

    def test_rehashed_missing_failed_cost_rejected(self):
        writer = self.writer()
        self.freeze(writer)
        self.request(writer)
        writer.complete(request_id="request-1", at=BASE+timedelta(seconds=2), elapsed=2,
                        actual_cost_usdt="0.01", failure_reason="PROVIDER_ERROR")
        writer.close(at=BASE+timedelta(seconds=3), elapsed=3)
        self.mutate(lambda rows: rows[3]["payload"].pop("actual_cost_usdt"))
        with self.assertRaisesRegex(AuditError, "RESPONSE_SCHEMA"):
            verify(self.path)

    def test_rehashed_immature_memory_rejected(self):
        self.normal()
        self.mutate(lambda rows: rows[5]["payload"]["memory"].append(
            {"id":"future-outcome", "matured_at":(BASE+timedelta(seconds=20)).isoformat()}))
        with self.assertRaisesRegex(AuditError, "IMMATURE_MEMORY"):
            verify(self.path)

    def test_rehashed_policy_version_rejected(self):
        self.normal()
        self.mutate(lambda rows: rows[2]["payload"].update(policy_version="new-unapproved-v2"))
        with self.assertRaisesRegex(AuditError, "UNAUTHORIZED_OR_CHANGED_POLICY"):
            verify(self.path)

    def test_rehashed_same_packet_fill_claim_rejected(self):
        self.normal()
        def forge(rows):
            rows.insert(-1, dict(seq=0, previous_hash="", at=(BASE+timedelta(seconds=6)).isoformat(),
                elapsed_seconds=6, kind="paper_execution",
                payload={"source_frame":"frame-0", "fill_frame":"frame-0", "fee":0}))
        self.mutate(forge)
        with self.assertRaisesRegex(AuditError, "UNKNOWN_EVENT_KIND"):
            verify(self.path)

    def test_rehashed_duplicate_request_rejected(self):
        self.normal()
        def forge(rows):
            extra = copy.deepcopy(rows[2])
            extra["at"] = (BASE+timedelta(seconds=1.5)).isoformat()
            extra["elapsed_seconds"] = 1.5
            rows.insert(3, extra)
        self.mutate(forge)
        with self.assertRaisesRegex(AuditError, "DUPLICATE_OR_UNKNOWN_REQUEST"):
            verify(self.path)


if __name__ == "__main__":
    unittest.main()
