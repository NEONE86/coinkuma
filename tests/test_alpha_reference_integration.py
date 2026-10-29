from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from one_trading.alpha_loop.reference.evidence import EvidenceLedger
from one_trading.alpha_loop.reference.integration import bind_and_activate, submit_calculated_evidence
from one_trading.alpha_loop.reference.research_loop import ResearchLoop, ExperimentSpec, ControlError, digest
from test_alpha_reference_evidence import case_parameters, settle


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 29, tzinfo=timezone.utc)
        self.loop = ResearchLoop(Path(self.temp.name) / "loop.sqlite",
            {"CF": {"cash_units": 100, "time_seconds": 100}}, lambda: self.now)
        self.addCleanup(self.loop.close)
        self.loop.register_experiment(ExperimentSpec("integrated", "cf-test", "CF", 1,
            self.now.isoformat(), "2026-12-01T00:00:00Z", 100, 100, "SYNTHETIC"))
        self.loop.record_data("integrated", digest({"synthetic": True}))
        self.now += timedelta(seconds=1)
        self.protocol = dict(policy="AI", baseline="PRICE", risk_fingerprint="risk", return_scale=.01)

    def evidence(self):
        return EvidenceLedger(bind_and_activate(self.loop, "integrated", **self.protocol))

    def test_binding_immutable(self):
        self.evidence()
        with self.assertRaises(ControlError):
            bind_and_activate(self.loop, "integrated", **dict(self.protocol, return_scale=.02))

    def test_cannot_bind_after_activation(self):
        self.loop.begin_forward("integrated")
        with self.assertRaises(ControlError):
            self.evidence()

    def test_registry_metadata_cannot_be_overridden(self):
        with self.assertRaises(ControlError):
            bind_and_activate(self.loop, "integrated", alpha_index=1, **self.protocol)

    def test_wrong_alpha_index_rejected(self):
        ledger = self.evidence()
        wrong = EvidenceLedger(replace(ledger.spec, alpha_index=2))
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, wrong)

    def test_future_scoring_clock_rejected(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, ledger)

    def test_complete_synthetic_pipeline_is_only_simulation_candidate(self):
        ledger = self.evidence()
        for i in range(60):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(result["control"]["display_state"], "SIMULATION_EVIDENCE_CANDIDATE_REVIEW_REQUIRED")
        self.assertTrue(result["receipt"]["alpha_allocation_verified"])
        self.assertFalse(result["receipt"]["input_attestation_verified"])
        self.assertFalse(result["control"]["edge_proven"])
        self.assertFalse(result["control"]["live_enabled"])

    def test_actual_raw_loss_pauses_instead_of_promoting(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0, own=-.50)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(result["control"]["state"], "PAUSED")

    def test_losing_prefix_cannot_be_removed_by_fresh_calculator(self):
        ledger = self.evidence()
        for i in range(30):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i, own=-.01)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        replacement = EvidenceLedger(ledger.spec)
        for i in range(30, 60):
            replacement.register_case(**case_parameters(replacement, i))
            settle(replacement, i)
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i)
        self.assertTrue(replacement.receipt()["statistical_gate_passed"])
        self.assertFalse(ledger.receipt()["statistical_gate_passed"])
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, replacement)
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(result["control"]["state"], "FORWARD_TEST")
        self.assertEqual(result["receipt"]["scored_clusters"], 60)
        self.assertEqual(result["receipt"]["alpha_index"], 1)

    def test_existing_outcome_cannot_disappear_while_case_is_preserved(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0, own=-.01)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        replacement = EvidenceLedger(ledger.spec)
        replacement.register_case(**case_parameters(replacement, 0))
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, replacement)

    def test_changed_outcome_rejected_even_from_valid_new_calculator(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0, own=-.01)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        replacement = EvidenceLedger(ledger.spec)
        replacement.register_case(**case_parameters(replacement, 0))
        settle(replacement, 0, own=.01)
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, replacement)

    def test_changed_case_and_reordered_identity_prefix_rejected(self):
        ledger = self.evidence()
        for i in range(2):
            ledger.register_case(**case_parameters(ledger, i))
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        changed = EvidenceLedger(ledger.spec)
        for i in range(2):
            case = case_parameters(changed, i)
            case["decision_hashes"]["AI"] = "changed-decision"
            changed.register_case(**case)
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, changed)
        reordered = EvidenceLedger(ledger.spec)
        for i in range(2):
            case = case_parameters(reordered, i)
            case["case_id"], case["cluster_id"] = f"case-{1-i}", f"cluster-{1-i}"
            reordered.register_case(**case)
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, reordered)

    def test_normal_append_and_exact_submission_are_idempotent(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        first = submit_calculated_evidence(self.loop, ledger)
        audit_count = self.loop.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        again = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(first, again)
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], audit_count)
        ledger.register_case(**case_parameters(ledger, 1))
        settle(ledger, 1)
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertTrue(result["receipt"]["record_continuity_verified"])
        for table in ("evidence_cases", "evidence_outcomes", "evidence_submissions"):
            self.assertEqual(self.loop.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 2)

    def test_terminal_candidate_resubmission_is_idempotent(self):
        ledger = self.evidence()
        for i in range(60):
            ledger.register_case(**case_parameters(ledger, i))
            settle(ledger, i)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        first = submit_calculated_evidence(self.loop, ledger)
        again = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(first, again)
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM evidence_submissions").fetchone()[0], 1)

    def test_resume_does_not_clear_the_same_cohort_tail_guard(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0, own=-.50)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        self.loop.resume("integrated")
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(result["control"]["state"], "PAUSED")
        self.assertEqual(result["receipt"]["raw_tail_breaches"], 1)
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM evidence_outcomes").fetchone()[0], 1)

    def test_submission_clock_cannot_regress_with_identical_outcomes(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        ledger.evaluation_as_of = "2026-10-31T00:00:00Z"
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        earlier = EvidenceLedger(ledger.spec)
        earlier.register_case(**case_parameters(earlier, 0))
        settle(earlier, 0)
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, earlier)

    def test_new_case_cannot_be_backfilled_before_submitted_cutoff(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        ledger.evaluation_as_of = "2026-10-31T00:00:00Z"
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        replacement = EvidenceLedger(ledger.spec)
        for i in range(2):
            replacement.register_case(**case_parameters(replacement, i))
            settle(replacement, i)
        replacement.evaluation_as_of = ledger.evaluation_as_of
        with self.assertRaises(ControlError):
            submit_calculated_evidence(self.loop, replacement)

    def test_mutable_score_cache_is_not_accepted_as_calculated_evidence(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        ledger.processed = 1000
        for stream in ledger.streams.values():
            stream.logs = [1000.0] * len(stream.logs)
        self.assertTrue(ledger.receipt()["statistical_gate_passed"])
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        result = submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(result["receipt"]["scored_clusters"], 1)
        self.assertFalse(result["receipt"]["statistical_gate_passed"])
        self.assertEqual(result["control"]["state"], "FORWARD_TEST")

    def test_committed_records_are_immutable(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        submit_calculated_evidence(self.loop, ledger)
        for table, field in (("evidence_cases", "payload_json"),
                             ("evidence_outcomes", "payload_json"),
                             ("evidence_submissions", "receipt_json")):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.loop.db.execute(f"UPDATE {table} SET {field}='changed'")
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.loop.db.execute(f"DELETE FROM {table}")

    def test_record_and_verdict_commit_is_atomic(self):
        ledger = self.evidence()
        ledger.register_case(**case_parameters(ledger, 0))
        settle(ledger, 0)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        original_audit = self.loop._audit

        def interrupted(action, *args, **kwargs):
            if action == "EVIDENCE_RECEIPT":
                raise RuntimeError("synthetic interruption before commit")
            return original_audit(action, *args, **kwargs)

        with patch.object(self.loop, "_audit", side_effect=interrupted), self.assertRaises(RuntimeError):
            submit_calculated_evidence(self.loop, ledger)
        for table in ("evidence_cases", "evidence_outcomes", "evidence_submissions"):
            self.assertEqual(self.loop.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.loop.status("integrated")["state"], "FORWARD_TEST")
        submit_calculated_evidence(self.loop, ledger)
        self.assertEqual(self.loop.db.execute("SELECT COUNT(*) FROM evidence_cases").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
