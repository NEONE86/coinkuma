"""Offline research-control reference. No network, models, orders, or proven alpha.

Money is an arbitrary integer accounting unit, NOT a real-funds allocation.
SQLite commits claims and reservations together. A crashed claim is never
automatically retried: reconcile the worker result before acknowledging it.
The alpha registry is global within THIS ledger, not across new/copied databases.
This is an authorization/cost ledger, not a watchdog that can cap provider bills.
An over-budget receipt preserves the reported charge and halts its engine pending
explicit external reconciliation; it never silently grants additional budget.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable
import uuid

UTC = timezone.utc
MODES = {"SYNTHETIC", "HISTORICAL", "FORWARD_PAPER"}
TERMINAL = {"EVIDENCE_CANDIDATE", "REJECTED", "INCONCLUSIVE"}
KINDS = {"due_label": 0, "cheap_falsifier": 1, "research": 2}


class ControlError(ValueError):
    pass


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ControlError("An explicit UTC timestamp is required")
    return dt.astimezone(UTC)


def stamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def units(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ControlError(f"{name} must be a known nonnegative integer")
    return value


def content_hash(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ControlError("Expected lowercase SHA-256 content hash")
    return value


@dataclass(frozen=True)
class ExperimentSpec:
    id: str
    lineage: str
    engine: str
    version: int
    preregistered_at: str
    deadline_at: str
    cash_budget_units: int
    time_budget_seconds: int
    evidence_mode: str

    def __post_init__(self):
        if any(not isinstance(x, str) or not x.strip() for x in (self.id, self.lineage, self.engine)):
            raise ControlError("id, lineage and engine must be nonempty strings")
        if type(self.version) is not int or self.version < 1:
            raise ControlError("version must be a positive integer")
        units(self.cash_budget_units, "cash_budget_units")
        units(self.time_budget_seconds, "time_budget_seconds")
        if self.evidence_mode not in MODES:
            raise ControlError("Unknown evidence mode")
        if utc(self.deadline_at) <= utc(self.preregistered_at):
            raise ControlError("deadline must be after preregistration")

    @property
    def hash(self) -> str:
        return digest(asdict(self))


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS engine_budgets (
 engine TEXT PRIMARY KEY, cash_limit INTEGER NOT NULL, time_limit INTEGER NOT NULL,
 cash_spent INTEGER NOT NULL DEFAULT 0, time_spent INTEGER NOT NULL DEFAULT 0,
 cash_reserved INTEGER NOT NULL DEFAULT 0, time_reserved INTEGER NOT NULL DEFAULT 0,
 engine_halted INTEGER NOT NULL DEFAULT 0, halt_reason TEXT);
CREATE TABLE IF NOT EXISTS experiments (
 id TEXT PRIMARY KEY, lineage TEXT NOT NULL, engine TEXT NOT NULL REFERENCES engine_budgets(engine),
 version INTEGER NOT NULL, spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL,
 registered_at TEXT NOT NULL, state TEXT NOT NULL, resume_state TEXT,
 cash_spent INTEGER NOT NULL DEFAULT 0, time_spent INTEGER NOT NULL DEFAULT 0,
 cash_reserved INTEGER NOT NULL DEFAULT 0, time_reserved INTEGER NOT NULL DEFAULT 0,
 forward_at TEXT, receipt_json TEXT, UNIQUE(lineage, version));
CREATE TABLE IF NOT EXISTS alpha_registry (
 j INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT UNIQUE NOT NULL REFERENCES experiments(id),
 alpha REAL NOT NULL, registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
 experiment_id TEXT NOT NULL REFERENCES experiments(id), content_hash TEXT NOT NULL,
 first_received_at TEXT NOT NULL, PRIMARY KEY(experiment_id, content_hash));
CREATE TABLE IF NOT EXISTS tasks (
 task_key TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
 kind TEXT NOT NULL, input_hash TEXT NOT NULL, slot TEXT NOT NULL,
 task_json TEXT NOT NULL, state TEXT NOT NULL, claim_token TEXT, claimed_at TEXT,
 result_json TEXT, actual_cash INTEGER, actual_time INTEGER, overrun_receipt_json TEXT,
 UNIQUE(experiment_id, kind, input_hash, slot));
CREATE TABLE IF NOT EXISTS audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
 experiment_id TEXT, payload_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS freeze_spec BEFORE UPDATE ON experiments
WHEN OLD.id != NEW.id OR OLD.lineage != NEW.lineage OR OLD.engine != NEW.engine
 OR OLD.version != NEW.version OR OLD.spec_json != NEW.spec_json
 OR OLD.spec_hash != NEW.spec_hash OR OLD.registered_at != NEW.registered_at
BEGIN SELECT RAISE(ABORT, 'immutable experiment specification'); END;
CREATE TRIGGER IF NOT EXISTS keep_experiments BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'preserve experiment history'); END;
CREATE TRIGGER IF NOT EXISTS freeze_alpha BEFORE UPDATE ON alpha_registry
BEGIN SELECT RAISE(ABORT, 'immutable alpha allocation'); END;
CREATE TRIGGER IF NOT EXISTS keep_alpha BEFORE DELETE ON alpha_registry
BEGIN SELECT RAISE(ABORT, 'alpha cannot be recycled'); END;
CREATE TRIGGER IF NOT EXISTS freeze_audit BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
CREATE TRIGGER IF NOT EXISTS keep_audit BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'append-only audit'); END;
CREATE TRIGGER IF NOT EXISTS freeze_observation BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'immutable observation receipt'); END;
CREATE TRIGGER IF NOT EXISTS keep_observation BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'preserve observation receipts'); END;
CREATE TRIGGER IF NOT EXISTS freeze_task BEFORE UPDATE ON tasks
WHEN OLD.task_key != NEW.task_key OR OLD.experiment_id != NEW.experiment_id
 OR OLD.kind != NEW.kind OR OLD.input_hash != NEW.input_hash
 OR OLD.slot != NEW.slot OR OLD.task_json != NEW.task_json
BEGIN SELECT RAISE(ABORT, 'immutable task specification'); END;
CREATE TRIGGER IF NOT EXISTS keep_task BEFORE DELETE ON tasks
BEGIN SELECT RAISE(ABORT, 'preserve task history'); END;
"""


class ResearchLoop:
    def __init__(self, db_path: str | Path, engine_budgets: dict | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.db = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            with self.transaction():
                for engine, budget in (engine_budgets or {}).items():
                    cash = units(budget.get("cash_units"), "engine cash_units")
                    seconds = units(budget.get("time_seconds"), "engine time_seconds")
                    old = self.db.execute("SELECT * FROM engine_budgets WHERE engine=?", (engine,)).fetchone()
                    if old and (old["cash_limit"], old["time_limit"]) != (cash, seconds):
                        raise ControlError("Reopening a ledger cannot reset or change its budget")
                    self.db.execute("INSERT OR IGNORE INTO engine_budgets(engine,cash_limit,time_limit) VALUES(?,?,?)",
                                    (engine, cash, seconds))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def now(self):
        return utc(self.clock())

    def _audit(self, action, experiment_id=None, **payload):
        self.db.execute("INSERT INTO audit(at,action,experiment_id,payload_json) VALUES(?,?,?,?)",
                        (stamp(self.now()), action, experiment_id, canonical(payload)))

    def _experiment(self, experiment_id):
        row = self.db.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            raise ControlError("Unknown experiment")
        return row, json.loads(row["spec_json"])

    def _expire(self, experiment_id):
        row, spec = self._experiment(experiment_id)
        if row["state"] not in TERMINAL and self.now() >= utc(spec["deadline_at"]):
            self.db.execute("UPDATE experiments SET state='INCONCLUSIVE' WHERE id=?", (experiment_id,))
            self._audit("DEADLINE_EXPIRED", experiment_id, reason="No verdict before immutable deadline")
            return True
        return False

    def _refresh_deadline(self, experiment_id):
        # Commit expiration separately: a subsequently rejected request must not
        # roll the deadline decision back to FORWARD_TEST.
        with self.transaction():
            self._expire(experiment_id)

    def register_experiment(self, spec: ExperimentSpec):
        if not isinstance(spec, ExperimentSpec):
            raise ControlError("Use an ExperimentSpec")
        with self.transaction():
            old = self.db.execute("SELECT spec_hash FROM experiments WHERE id=?", (spec.id,)).fetchone()
            if old:
                if old[0] != spec.hash:
                    raise ControlError("Experiment id is immutable; use a new version and id")
                return self.status(spec.id)
            if utc(spec.preregistered_at) > self.now() or utc(spec.deadline_at) <= self.now():
                raise ControlError("Registration must occur before deadline, not before declared preregistration")
            if not self.db.execute("SELECT 1 FROM engine_budgets WHERE engine=?", (spec.engine,)).fetchone():
                raise ControlError("Engine has no independent budget")
            try:
                self.db.execute("INSERT INTO experiments(id,lineage,engine,version,spec_json,spec_hash,registered_at,state) "
                                "VALUES(?,?,?,?,?,?,?,'PROPOSED')",
                                (spec.id, spec.lineage, spec.engine, spec.version, canonical(asdict(spec)),
                                 spec.hash, stamp(self.now())))
            except sqlite3.IntegrityError as exc:
                raise ControlError("Duplicate lineage/version is not a new experiment") from exc
            self._audit("EXPERIMENT_REGISTERED", spec.id, spec_hash=spec.hash,
                        declared_preregistered_at=spec.preregistered_at,
                        actual_registered_at=stamp(self.now()))
        return self.status(spec.id)

    def record_data(self, experiment_id: str, payload_hash: str) -> bool:
        content_hash(payload_hash)
        self._refresh_deadline(experiment_id)
        with self.transaction():
            row, _ = self._experiment(experiment_id)
            if row["state"] in TERMINAL:
                raise ControlError("Terminal experiment cannot accept new evidence")
            cur = self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)",
                                  (experiment_id, payload_hash, stamp(self.now())))
            if not cur.rowcount:
                return False
            if row["state"] == "PROPOSED":
                self.db.execute("UPDATE experiments SET state='DATA_READY' WHERE id=?", (experiment_id,))
            self._audit("DATA_RECEIVED", experiment_id, content_hash=payload_hash,
                        note="Different bytes are not proof of independent information")
            return True

    def begin_forward(self, experiment_id: str):
        self._refresh_deadline(experiment_id)
        with self.transaction():
            row, spec = self._experiment(experiment_id)
            existing = self.db.execute("SELECT * FROM alpha_registry WHERE experiment_id=?", (experiment_id,)).fetchone()
            if existing:
                return dict(existing)
            if row["state"] != "DATA_READY":
                raise ControlError("Forward registration requires DATA_READY")
            if spec["evidence_mode"] == "HISTORICAL":
                raise ControlError("Historical work needs a new preregistered forward specification")
            if self.now() <= max(utc(spec["preregistered_at"]), utc(row["registered_at"])):
                raise ControlError("Forward activation must be later than actual and declared preregistration")
            j = self.db.execute("SELECT COALESCE(MAX(j),0)+1 FROM alpha_registry").fetchone()[0]
            alpha = 0.05 / (j * (j + 1))
            self.db.execute("INSERT INTO alpha_registry VALUES(?,?,?,?)",
                            (j, experiment_id, alpha, stamp(self.now())))
            self.db.execute("UPDATE experiments SET state='FORWARD_TEST',forward_at=? WHERE id=?",
                            (stamp(self.now()), experiment_id))
            self._audit("FORWARD_REGISTERED", experiment_id, alpha_index=j, alpha=alpha,
                        evidence_mode=spec["evidence_mode"], note="Alpha allocation is not evidence")
        return dict(self.db.execute("SELECT * FROM alpha_registry WHERE experiment_id=?", (experiment_id,)).fetchone())

    def suspend(self, experiment_id: str, state: str, reason: str):
        if state not in {"PAUSED", "DATA_BLOCKED"} or not reason:
            raise ControlError("Supply PAUSED or DATA_BLOCKED and a reason")
        self._refresh_deadline(experiment_id)
        with self.transaction():
            row, _ = self._experiment(experiment_id)
            if row["state"] in TERMINAL:
                raise ControlError("Terminal experiment cannot be suspended")
            resume = row["resume_state"] if row["state"] in {"PAUSED", "DATA_BLOCKED"} else row["state"]
            self.db.execute("UPDATE experiments SET state=?,resume_state=? WHERE id=?", (state, resume, experiment_id))
            self._audit("SUSPENDED", experiment_id, state=state, reason=reason)

    def resume(self, experiment_id: str):
        self._refresh_deadline(experiment_id)
        with self.transaction():
            row, _ = self._experiment(experiment_id)
            if row["state"] not in {"PAUSED", "DATA_BLOCKED"}:
                raise ControlError("Only a suspended experiment may resume")
            self.db.execute("UPDATE experiments SET state=?,resume_state=NULL WHERE id=?",
                            (row["resume_state"], experiment_id))
            self._audit("RESUMED", experiment_id, state=row["resume_state"])

    def enqueue_task(self, task_key: str, experiment_id: str, kind: str, *, input_hash: str,
                     cash_units: int, time_seconds: int, decision_value_score: float = 0,
                     due_at: str | None = None, slot: str = ""):
        if not isinstance(task_key, str) or not task_key or kind not in KINDS:
            raise ControlError("Known task kind and nonempty key are required")
        cash = units(cash_units, "task cash_units")
        seconds = units(time_seconds, "task time_seconds")
        content_hash(input_hash)
        if isinstance(decision_value_score, bool) or not isinstance(decision_value_score, (int, float)) \
                or not math.isfinite(decision_value_score) or decision_value_score < 0:
            raise ControlError("A finite nonnegative heuristic score is required")
        if kind == "due_label" and (due_at is None or not slot):
            raise ControlError("Label tasks need their immutable maturity time and forecast/horizon slot")
        if kind != "due_label" and slot:
            raise ControlError("Changing a slot cannot make duplicated research input fresh")
        due = stamp(utc(due_at)) if due_at else None
        payload = dict(task_key=task_key, experiment_id=experiment_id, kind=kind, input_hash=input_hash,
                       cash_units=cash, time_seconds=seconds, decision_value_score=decision_value_score,
                       due_at=due, slot=slot, score_semantics="HEURISTIC_NOT_ESTIMATED_TRUE_EV")
        self._refresh_deadline(experiment_id)
        with self.transaction():
            old = self.db.execute("SELECT task_json FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if old:
                if old[0] != canonical(payload):
                    raise ControlError("Task key cannot be reused with changed inputs or costs")
                return self.get_task(task_key)
            row, _ = self._experiment(experiment_id)
            if row["state"] in TERMINAL:
                raise ControlError("Terminal experiment cannot enqueue work")
            if not self.db.execute("SELECT 1 FROM observations WHERE experiment_id=? AND content_hash=?",
                                   (experiment_id, input_hash)).fetchone():
                raise ControlError("Task input must have a recorded content receipt")
            if kind == "due_label" and row["forward_at"] is None:
                raise ControlError("Forward label task requires forward registration")
            if kind == "due_label" and utc(due) < utc(row["forward_at"]):
                raise ControlError("Forward label maturity cannot precede forward activation")
            try:
                self.db.execute("INSERT INTO tasks(task_key,experiment_id,kind,input_hash,slot,task_json,state) "
                                "VALUES(?,?,?,?,?,?,'QUEUED')",
                                (task_key, experiment_id, kind, input_hash, slot, canonical(payload)))
            except sqlite3.IntegrityError as exc:
                raise ControlError("This input/kind/slot already has a task; no duplicate spending") from exc
            self._audit("TASK_ENQUEUED", experiment_id, task=payload)
        return self.get_task(task_key)

    def _affordable(self, row, spec, engine, task):
        return (row["cash_spent"] + row["cash_reserved"] + task["cash_units"] <= spec["cash_budget_units"]
                and row["time_spent"] + row["time_reserved"] + task["time_seconds"] <= spec["time_budget_seconds"]
                and engine["cash_spent"] + engine["cash_reserved"] + task["cash_units"] <= engine["cash_limit"]
                and engine["time_spent"] + engine["time_reserved"] + task["time_seconds"] <= engine["time_limit"])

    def tick(self):
        """Claim at most one task. A no-work heartbeat consumes no budget or log rows.

        Due labels precede optional research; unaffordable due labels block only
        optional work in their own engine. The demo labels are deterministic and
        zero-cost. Production paid outcome collection needs an operational reserve.
        """
        with self.transaction():
            for row in self.db.execute("SELECT id FROM experiments WHERE state NOT IN ('REJECTED','INCONCLUSIVE','EVIDENCE_CANDIDATE')").fetchall():
                self._expire(row[0])
            eligible = []
            for queued in self.db.execute("SELECT * FROM tasks WHERE state='QUEUED'").fetchall():
                row, spec = self._experiment(queued["experiment_id"])
                if row["state"] not in {"DATA_READY", "FORWARD_TEST"}:
                    continue
                task = json.loads(queued["task_json"])
                if task["due_at"] is not None and utc(task["due_at"]) > self.now():
                    continue
                engine = self.db.execute("SELECT * FROM engine_budgets WHERE engine=?", (row["engine"],)).fetchone()
                if engine["engine_halted"] and not (task["kind"] == "due_label"
                        and task["cash_units"] == 0 and task["time_seconds"] == 0):
                    continue
                cost = task["cash_units"] / max(engine["cash_limit"], 1) + task["time_seconds"] / max(engine["time_limit"], 1)
                ratio = task["decision_value_score"] / max(cost, 1e-12)
                # Due-label order is chronological. Other ties are stable task keys.
                sort_key = (KINDS[task["kind"]], (task["due_at"] or "") if task["kind"] == "due_label" else "",
                            -ratio, task["task_key"])
                eligible.append((sort_key, row, spec, engine, task))
            engines_with_unfunded_labels = set()
            for _, row, spec, engine, task in sorted(eligible, key=lambda item: item[0]):
                if task["kind"] != "due_label" and row["engine"] in engines_with_unfunded_labels:
                    continue
                if not self._affordable(row, spec, engine, task):
                    if task["kind"] == "due_label":
                        engines_with_unfunded_labels.add(row["engine"])
                    continue
                token = uuid.uuid4().hex
                self.db.execute("UPDATE tasks SET state='CLAIMED',claim_token=?,claimed_at=? WHERE task_key=?",
                                (token, stamp(self.now()), task["task_key"]))
                for table, key, value in (("experiments", "id", row["id"]), ("engine_budgets", "engine", row["engine"])):
                    self.db.execute(f"UPDATE {table} SET cash_reserved=cash_reserved+?,time_reserved=time_reserved+? WHERE {key}=?",
                                    (task["cash_units"], task["time_seconds"], value))
                self._audit("TASK_CLAIMED", row["id"], task_key=task["task_key"], claim_token=token,
                            cash_reserved=task["cash_units"], time_reserved=task["time_seconds"])
                return self.get_task(task["task_key"])
        return None

    def ack(self, task_key: str, claim_token: str, *, result: dict,
            actual_cash_units: int, actual_time_seconds: int, succeeded: bool = True):
        cash = units(actual_cash_units, "actual_cash_units")
        seconds = units(actual_time_seconds, "actual_time_seconds")
        if not isinstance(result, dict) or type(succeeded) is not bool:
            raise ControlError("A structured result and boolean succeeded are required")
        result_json = canonical(result)
        desired = "DONE" if succeeded else "FAILED"
        over_budget = False
        with self.transaction():
            row = self.db.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
            if not row or not claim_token or row["claim_token"] != claim_token:
                raise ControlError("Claim token mismatch")
            if row["state"] in {"DONE", "FAILED"}:
                if (row["state"], row["result_json"], row["actual_cash"], row["actual_time"]) != (desired, result_json, cash, seconds):
                    raise ControlError("Acknowledged work is immutable")
                return self.get_task(task_key)
            if row["state"] != "CLAIMED":
                raise ControlError("Only claimed work can be acknowledged")
            if row["overrun_receipt_json"] is not None:
                raise ControlError("Previously reported overrun requires explicit external reconciliation; no replacement ack")
            task = json.loads(row["task_json"])
            experiment, _ = self._experiment(row["experiment_id"])
            if cash > task["cash_units"] or seconds > task["time_seconds"]:
                receipt = dict(task_key=task_key, actual_cash_units=cash, actual_time_seconds=seconds,
                               authorized_cash_units=task["cash_units"], authorized_time_seconds=task["time_seconds"],
                               reported_result=result, succeeded=succeeded,
                               accounting="UNRECONCILED_ACTUALS_ORIGINAL_RESERVATION_RETAINED")
                self.db.execute("UPDATE tasks SET overrun_receipt_json=? WHERE task_key=?",
                                (canonical(receipt), task_key))
                self.db.execute("UPDATE engine_budgets SET engine_halted=1,halt_reason=? WHERE engine=?",
                                (f"OVER_BUDGET_RECEIPT:{task_key}; external reconciliation required", experiment["engine"]))
                self._audit("OVER_BUDGET_RECEIPT", row["experiment_id"], **receipt)
                over_budget = True
            else:
                for table, key, value in (("experiments", "id", experiment["id"]), ("engine_budgets", "engine", experiment["engine"])):
                    self.db.execute(f"UPDATE {table} SET cash_reserved=cash_reserved-?,time_reserved=time_reserved-?,"
                                    f"cash_spent=cash_spent+?,time_spent=time_spent+? WHERE {key}=?",
                                    (task["cash_units"], task["time_seconds"], cash, seconds, value))
                self.db.execute("UPDATE tasks SET state=?,result_json=?,actual_cash=?,actual_time=? WHERE task_key=?",
                                (desired, result_json, cash, seconds, task_key))
                self._audit("TASK_ACKNOWLEDGED", row["experiment_id"], task_key=task_key, state=desired,
                            actual_cash_units=cash, actual_time_seconds=seconds, result_hash=digest(result))
        if over_budget:
            # Raise AFTER commit so disclosed charges and the halt cannot vanish.
            raise ControlError("Over-budget actuals recorded; engine halted and original claim reserved pending external reconciliation")
        return self.get_task(task_key)

    def record_evidence_verdict(self, experiment_id: str, verdict: str, evidence_ref: str,
                                *, verified_receipt: bool = False):
        """Trust boundary, NOT an evidence-math verifier.

        Raw flags only append an audit record. verified_receipt=True is a caller
        attestation; it must be set only by a separately audited local verifier.
        Even then, pass creates REVIEW_REQUIRED, never live or edge_proven.
        """
        if verdict not in {"pending", "pass", "reject"} or not evidence_ref or type(verified_receipt) is not bool:
            raise ControlError("Invalid evidence receipt")
        self._refresh_deadline(experiment_id)
        with self.transaction():
            row, spec = self._experiment(experiment_id)
            receipt = dict(verdict=verdict, evidence_ref=evidence_ref, verified_receipt=verified_receipt,
                           trust="CALLER_ATTESTATION_NOT_CONTROL_VERIFIED", evidence_mode=spec["evidence_mode"])
            if row["state"] in TERMINAL:
                if row["receipt_json"] == canonical(receipt):
                    return self.status(experiment_id)
                raise ControlError("Terminal verdict cannot be replaced")
            if verified_receipt and verdict == "pass" and row["state"] != "FORWARD_TEST":
                raise ControlError("Candidate requires an activated forward experiment")
            self._audit("EVIDENCE_RECEIPT", experiment_id, **receipt)
            if verified_receipt and verdict in {"pass", "reject"}:
                state = "EVIDENCE_CANDIDATE" if verdict == "pass" else "REJECTED"
                self.db.execute("UPDATE experiments SET state=?,receipt_json=? WHERE id=?",
                                (state, canonical(receipt), experiment_id))
        return self.status(experiment_id)

    def status(self, experiment_id: str):
        row, spec = self._experiment(experiment_id)
        alpha = self.db.execute("SELECT * FROM alpha_registry WHERE experiment_id=?", (experiment_id,)).fetchone()
        display = "EVIDENCE_CANDIDATE_REVIEW_REQUIRED" if row["state"] == "EVIDENCE_CANDIDATE" else row["state"]
        if spec["evidence_mode"] == "SYNTHETIC":
            display = "SIMULATION_" + display
        return dict(experiment_id=experiment_id, spec=spec, spec_hash=row["spec_hash"], state=row["state"],
                    display_state=display, registered_at=row["registered_at"], forward_at=row["forward_at"],
                    alpha=dict(alpha) if alpha else None, edge_proven=False, live_enabled=False,
                    cash_spent=row["cash_spent"], cash_reserved=row["cash_reserved"],
                    time_spent=row["time_spent"], time_reserved=row["time_reserved"],
                    observations=self.db.execute("SELECT COUNT(*) FROM observations WHERE experiment_id=?", (experiment_id,)).fetchone()[0])

    def get_task(self, task_key: str):
        row = self.db.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if row is None:
            raise ControlError("Unknown task")
        result = dict(row)
        result["task"] = json.loads(result.pop("task_json"))
        return result

    def engine_status(self, engine: str):
        row = self.db.execute("SELECT * FROM engine_budgets WHERE engine=?", (engine,)).fetchone()
        if row is None:
            raise ControlError("Unknown engine")
        status = dict(row)
        status["unreconciled_overruns"] = [json.loads(receipt[0]) for receipt in self.db.execute(
            "SELECT t.overrun_receipt_json FROM tasks t JOIN experiments e ON t.experiment_id=e.id "
            "WHERE e.engine=? AND t.overrun_receipt_json IS NOT NULL ORDER BY t.task_key", (engine,))]
        return status


def demo(db_path: str | Path):
    """Synthetic demonstrator: no actual market data, paid compute, or trading."""
    moment = [datetime(2026, 10, 29, 0, 0, tzinfo=UTC)]
    loop = ResearchLoop(db_path, {"CF": {"cash_units": 100, "time_seconds": 100},
                                  "GROWTH": {"cash_units": 100, "time_seconds": 100}}, clock=lambda: moment[0])
    spec = ExperimentSpec("synthetic-demo", "demo-family", "CF", 1, stamp(moment[0]),
                          stamp(moment[0] + timedelta(days=1)), 100, 100, "SYNTHETIC")
    loop.register_experiment(spec)
    sample = digest({"label": "SYNTHETIC_NOT_MARKET_DATA", "sample": 1})
    loop.record_data(spec.id, sample)
    moment[0] += timedelta(seconds=1)
    loop.begin_forward(spec.id)
    loop.enqueue_task("demo-research", spec.id, "research", input_hash=sample,
                      cash_units=5, time_seconds=5, decision_value_score=10)
    loop.enqueue_task("demo-label", spec.id, "due_label", input_hash=sample, cash_units=0,
                      time_seconds=0, due_at=stamp(moment[0]), slot="synthetic-forecast-1/24h")
    selected = []
    while task := loop.tick():
        selected.append(task["task_key"])
        loop.ack(task["task_key"], task["claim_token"], result={"synthetic": True},
                 actual_cash_units=task["task"]["cash_units"], actual_time_seconds=task["task"]["time_seconds"])
    # Deliberately untrusted pass: it MUST NOT move to candidate or proven edge.
    loop.record_evidence_verdict(spec.id, "pass", "synthetic-untrusted-flag")
    result = {"scope": "OFFLINE_SYNTHETIC_CONTROL_DEMONSTRATION", "selected_order": selected,
              "status": loop.status(spec.id), "budget": loop.engine_status("CF")}
    loop.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Run a synthetic offline demonstration")
    parser.add_argument("--db", type=Path, help="New SQLite ledger path; demo otherwise uses a temporary directory")
    args = parser.parse_args()
    if not args.demo:
        parser.error("Only --demo is exposed; this is not a market worker")
    if args.db:
        if args.db.exists():
            parser.error("Demo requires a new ledger; it will not reset an existing one")
        result = demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="one-loop-demo-") as temp:
            result = demo(Path(temp) / "research.sqlite")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
