"""Bind the offline evidence calculator to ONE immutable research ledger.

This verifies identifiers, frozen scoring contracts, global alpha allocations,
and append-only continuity of previously submitted cases and outcomes.
It DOES NOT authenticate upstream prices, costs, timestamps or source ownership.
No broker, no network, no real-money policy activation.
"""
from dataclasses import asdict
import json

from .evidence import EvidenceLedger, EvidenceSpec, Metrics, canonical_hash, instant
from .research_loop import ControlError, TERMINAL, canonical, stamp


BINDINGS = """
CREATE TABLE IF NOT EXISTS evidence_bindings (
 experiment_id TEXT PRIMARY KEY REFERENCES experiments(id),
 protocol_json TEXT NOT NULL, protocol_hash TEXT NOT NULL,
 bound_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS freeze_evidence_binding BEFORE UPDATE ON evidence_bindings
BEGIN SELECT RAISE(ABORT, 'evidence protocol cannot change'); END;
CREATE TRIGGER IF NOT EXISTS keep_evidence_binding BEFORE DELETE ON evidence_bindings
BEGIN SELECT RAISE(ABORT, 'evidence protocol cannot be removed'); END;
CREATE TABLE IF NOT EXISTS evidence_cases (
 experiment_id TEXT NOT NULL REFERENCES experiments(id), sequence INTEGER NOT NULL,
 case_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 PRIMARY KEY(experiment_id, case_id), UNIQUE(experiment_id, sequence));
CREATE TABLE IF NOT EXISTS evidence_outcomes (
 experiment_id TEXT NOT NULL, case_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 PRIMARY KEY(experiment_id, case_id),
 FOREIGN KEY(experiment_id, case_id) REFERENCES evidence_cases(experiment_id, case_id));
CREATE TABLE IF NOT EXISTS evidence_submissions (
 experiment_id TEXT NOT NULL REFERENCES experiments(id), sequence INTEGER NOT NULL,
 snapshot_hash TEXT NOT NULL, evaluation_as_of TEXT NOT NULL,
 receipt_json TEXT NOT NULL, receipt_hash TEXT NOT NULL, submitted_at TEXT NOT NULL,
 PRIMARY KEY(experiment_id, sequence), UNIQUE(experiment_id, snapshot_hash));
CREATE TRIGGER IF NOT EXISTS freeze_evidence_cases BEFORE UPDATE ON evidence_cases
BEGIN SELECT RAISE(ABORT, 'submitted cases cannot change'); END;
CREATE TRIGGER IF NOT EXISTS keep_evidence_cases BEFORE DELETE ON evidence_cases
BEGIN SELECT RAISE(ABORT, 'submitted cases cannot be removed'); END;
CREATE TRIGGER IF NOT EXISTS freeze_evidence_outcomes BEFORE UPDATE ON evidence_outcomes
BEGIN SELECT RAISE(ABORT, 'submitted outcomes cannot change'); END;
CREATE TRIGGER IF NOT EXISTS keep_evidence_outcomes BEFORE DELETE ON evidence_outcomes
BEGIN SELECT RAISE(ABORT, 'submitted outcomes cannot be removed'); END;
CREATE TRIGGER IF NOT EXISTS freeze_evidence_submissions BEFORE UPDATE ON evidence_submissions
BEGIN SELECT RAISE(ABORT, 'evidence submission history cannot change'); END;
CREATE TRIGGER IF NOT EXISTS keep_evidence_submissions BEFORE DELETE ON evidence_submissions
BEGIN SELECT RAISE(ABORT, 'evidence submission history cannot be removed'); END;
"""


def protocol_of(spec):
    result = asdict(spec)
    for key in ("alpha_index", "registered_at"):
        result.pop(key)
    return result


def bind_and_activate(loop, experiment_id, **protocol):
    """Freeze a scoring protocol before activation, then attach ledger alpha.

    Required protocol fields: policy, baseline, risk_fingerprint, return_scale.
    Version, mode, actual registration time and alpha cannot be caller overridden.
    """
    if set(protocol) & {"experiment_id", "version", "alpha_index", "registered_at", "mode", "alpha_total"}:
        raise ControlError("Registry metadata cannot be overridden by a scoring protocol")
    status = loop.status(experiment_id)
    validated = EvidenceSpec(experiment_id=experiment_id, version=str(status["spec"]["version"]),
                             alpha_index=1, registered_at=status["registered_at"],
                             mode=status["spec"]["evidence_mode"], alpha_total=.05, **protocol)
    frozen = protocol_of(validated)
    frozen_json = json.dumps(frozen, sort_keys=True, separators=(",", ":"), allow_nan=False)
    fingerprint = canonical_hash(frozen)
    loop.db.executescript(BINDINGS)
    with loop.transaction():
        existing = loop.db.execute("SELECT * FROM evidence_bindings WHERE experiment_id=?", (experiment_id,)).fetchone()
        if existing:
            if existing["protocol_hash"] != fingerprint:
                raise ControlError("Scoring contract already frozen; create a new experiment")
        else:
            if status["state"] != "DATA_READY" or status["forward_at"] is not None:
                raise ControlError("Bind evidence before activating the forward cohort")
            loop.db.execute("INSERT INTO evidence_bindings VALUES(?,?,?,?)",
                            (experiment_id, frozen_json, fingerprint, stamp(loop.now())))
            loop._audit("EVIDENCE_PROTOCOL_BOUND", experiment_id, protocol_hash=fingerprint)
    allocation = loop.begin_forward(experiment_id)
    return EvidenceSpec(experiment_id=experiment_id, version=str(status["spec"]["version"]),
                        alpha_index=allocation["j"], registered_at=status["registered_at"],
                        mode=status["spec"]["evidence_mode"], alpha_total=.05, **protocol)


def _recalculate(ledger):
    """Snapshot records and rebuild arithmetic; never trust mutable E-score caches."""
    if not isinstance(ledger, EvidenceLedger):
        raise ControlError("Use an EvidenceLedger with explicit source records")
    cases = json.loads(canonical(ledger.cases))
    outcomes = json.loads(canonical(ledger.outcomes))
    rebuilt = EvidenceLedger(ledger.spec)
    for case in cases:
        if not isinstance(case.get("case_id"), str) or not case["case_id"]:
            raise ControlError("Case identity must be a nonempty string")
        rebuilt.register_case(**case)
    for case_id, observation in outcomes.items():
        if observation.get("case_id") != case_id or set(observation) != {
                "case_id", "metrics", "received_at", "source_hash", "mode"}:
            raise ControlError("Outcome identity/schema mismatch")
        rebuilt.resolve(case_id=case_id,
                        metrics={k: Metrics(**v) for k, v in observation["metrics"].items()},
                        received_at=observation["received_at"], as_of=ledger.evaluation_as_of,
                        source_hash=observation["source_hash"], mode=observation["mode"])
    if instant(rebuilt.evaluation_as_of) != instant(ledger.evaluation_as_of):
        raise ControlError("Evidence clock must be supported by its outcome records")
    return cases, outcomes, rebuilt.receipt()


def _preserve_prefix(loop, experiment_id, cases, outcomes, receipt):
    """Called inside the same write transaction as the verdict transition."""
    old_cases = loop.db.execute(
        "SELECT * FROM evidence_cases WHERE experiment_id=? ORDER BY sequence", (experiment_id,)).fetchall()
    if len(cases) < len(old_cases):
        raise ControlError("Previously submitted cases cannot be omitted")
    for index, old in enumerate(old_cases):
        if old["payload_hash"] != canonical_hash(cases[index]) or old["payload_json"] != canonical(cases[index]):
            raise ControlError("Previously submitted case prefix cannot change or be reordered")
    old_outcomes = {r["case_id"]: r for r in loop.db.execute(
        "SELECT * FROM evidence_outcomes WHERE experiment_id=?", (experiment_id,)).fetchall()}
    if not set(old_outcomes).issubset(outcomes):
        raise ControlError("Previously submitted outcomes cannot be omitted")
    for case_id, old in old_outcomes.items():
        if old["payload_hash"] != canonical_hash(outcomes[case_id]) or old["payload_json"] != canonical(outcomes[case_id]):
            raise ControlError("Previously submitted outcome cannot change")
    previous = loop.db.execute(
        "SELECT * FROM evidence_submissions WHERE experiment_id=? ORDER BY sequence DESC LIMIT 1",
        (experiment_id,)).fetchone()
    if previous:
        if instant(receipt["evaluation_as_of"]) < instant(previous["evaluation_as_of"]):
            raise ControlError("Submitted evaluation prefix cannot move backwards")
        if any(instant(c["decision_at"]) <= instant(previous["evaluation_as_of"])
               for c in cases[len(old_cases):]):
            raise ControlError("New cases must be decided after the previous submitted evidence cutoff")
    snapshot_hash = canonical_hash({"cases": cases, "outcomes": outcomes,
                                    "evaluation_as_of": receipt["evaluation_as_of"],
                                    "spec_hash": receipt["spec_hash"]})
    duplicate = bool(previous and previous["snapshot_hash"] == snapshot_hash)
    if not duplicate:
        for index, case in enumerate(cases[len(old_cases):], start=len(old_cases)):
            loop.db.execute("INSERT INTO evidence_cases VALUES(?,?,?,?,?)",
                            (experiment_id, index, case["case_id"], canonical(case), canonical_hash(case)))
        for case_id, observation in outcomes.items():
            if case_id not in old_outcomes:
                loop.db.execute("INSERT INTO evidence_outcomes VALUES(?,?,?,?)",
                                (experiment_id, case_id, canonical(observation), canonical_hash(observation)))
        loop.db.execute("INSERT INTO evidence_submissions VALUES(?,?,?,?,?,?,?)",
                        (experiment_id, previous["sequence"] + 1 if previous else 0, snapshot_hash,
                         receipt["evaluation_as_of"], canonical(receipt), canonical_hash(receipt), stamp(loop.now())))
    return duplicate


def submit_calculated_evidence(loop, ledger: EvidenceLedger):
    """Recalculate and commit records/verdict atomically; never activate real funds.

    Record continuity is local integrity, not authentication of prices or clocks.
    A disclosed raw loss remains in every later snapshot of this cohort; resuming
    the controller cannot clear it or restart the evidence process at one.
    """
    cases, outcomes, receipt = _recalculate(ledger)
    spec = ledger.spec
    loop._refresh_deadline(spec.experiment_id)
    with loop.transaction():
        control = loop.status(spec.experiment_id)
        allocation = control["alpha"]
        if not allocation or allocation["j"] != spec.alpha_index or allocation["alpha"] != spec.alpha:
            raise ControlError("Evidence calculation is not tied to this ledger alpha allocation")
        if str(control["spec"]["version"]) != spec.version or control["spec"]["evidence_mode"] != spec.mode:
            raise ControlError("Scoring version/origin mismatch")
        if instant(control["registered_at"]) != instant(spec.registered_at):
            raise ControlError("Actual registration timestamp mismatch")
        try:
            frozen = loop.db.execute("SELECT * FROM evidence_bindings WHERE experiment_id=?", (spec.experiment_id,)).fetchone()
        except Exception as exc:
            raise ControlError("Missing frozen evidence protocol") from exc
        if not frozen or frozen["protocol_hash"] != canonical_hash(protocol_of(spec)):
            raise ControlError("Scoring contract was not frozen before forward activation")
        if instant(frozen["bound_at"]) > instant(control["forward_at"]):
            raise ControlError("Late scoring-contract binding")
        if any(instant(case["decision_at"]) <= instant(control["forward_at"]) for case in cases):
            raise ControlError("Case predates actual forward activation")
        if instant(receipt["evaluation_as_of"]) > loop.now():
            raise ControlError("Evidence clock exceeds controller clock")
        receipt["alpha_allocation_verified"] = True
        receipt["registration_time_verified"] = True
        receipt["record_continuity_verified"] = True
        receipt["verification_scope"] = "LOCAL_BINDINGS_ARITHMETIC_AND_APPEND_ONLY_CONTINUITY_NOT_MARKET_ATTESTATION"
        receipt_hash = canonical_hash(receipt)
        verdict = "pass" if receipt["raw_tail_guard_passed"] and receipt["statistical_gate_passed"] else "pending"
        verdict_record = dict(verdict=verdict, evidence_ref=receipt_hash, verified_receipt=True,
                              trust="CALLER_ATTESTATION_NOT_CONTROL_VERIFIED", evidence_mode=spec.mode)
        row, _ = loop._experiment(spec.experiment_id)
        if row["state"] in TERMINAL and row["receipt_json"] != canonical(verdict_record):
            raise ControlError("Terminal verdict cannot be replaced")
        if row["state"] not in TERMINAL and verdict == "pass" and row["state"] != "FORWARD_TEST":
            raise ControlError("Candidate requires an activated forward experiment")
        duplicate = _preserve_prefix(loop, spec.experiment_id, cases, outcomes, receipt)
        # Keep the control ledger's terminal/idempotence rules, but make the
        # verdict and source-record persistence one transaction, not two commits.
        if row["state"] not in TERMINAL:
            if not receipt["raw_tail_guard_passed"] and row["state"] != "PAUSED":
                resume = row["resume_state"] if row["state"] == "DATA_BLOCKED" else row["state"]
                loop.db.execute("UPDATE experiments SET state='PAUSED',resume_state=? WHERE id=?",
                                (resume, spec.experiment_id))
                loop._audit("SUSPENDED", spec.experiment_id, state="PAUSED",
                            reason="Persistent raw tail-limit breach; no statistical override")
                duplicate = False  # Resuming must not evade the existing guard.
            if not duplicate:
                loop._audit("EVIDENCE_RECEIPT", spec.experiment_id, **verdict_record)
            if verdict == "pass":
                loop.db.execute("UPDATE experiments SET state='EVIDENCE_CANDIDATE',receipt_json=? WHERE id=?",
                                (canonical(verdict_record), spec.experiment_id))
        state = loop.status(spec.experiment_id)
    return {"receipt": receipt, "control": state}
