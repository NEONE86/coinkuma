"""Append-only prospective decision receipts; no model client or order router.

An operator supplies already received input and independently obtained model
responses.  All clocks are explicit to make offline attacks reproducible.  Live
source identity must additionally be verified against the original Bybit files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .engine import Frame, Plan, ReplayError, canonical, instant, sha256


VERSION = "ONE_PROSPECTIVE_DECISIONS_V1"
ZERO = "0" * 64
MAX_REQUEST_AGE_SECONDS = 5.0
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


def money(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ReplayError("COST_MUST_BE_EXPLICIT_DECIMAL_STRING")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ReplayError("BAD_COST") from exc
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -8:
        raise ReplayError("BAD_COST")
    return result


def identity(value: str) -> str:
    if not isinstance(value, str) or IDENT.fullmatch(value) is None:
        raise ReplayError("BAD_IDENTITY")
    return value


def stamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReplayError("UTC_TIME_REQUIRED")
    return value.astimezone(timezone.utc).isoformat()


def policy_registry(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(registry, dict) or not registry or "CASH" not in registry:
        raise ReplayError("CASH_POLICY_REQUIRED")
    normalized = {}
    for name, entry in registry.items():
        identity(name)
        if not isinstance(entry, dict) or set(entry) != {
            "version", "model", "prompt_sha256", "call_limit", "cost_limit_usdt"
        }:
            raise ReplayError("POLICY_REGISTRY_SCHEMA")
        identity(entry["version"])
        model = entry["model"]
        if model is not None:
            identity(model)
            if not isinstance(entry["prompt_sha256"], str) or not HEX.fullmatch(entry["prompt_sha256"]):
                raise ReplayError("BAD_PROMPT_IDENTITY")
        elif entry["prompt_sha256"] is not None:
            raise ReplayError("CONTROL_HAS_MODEL_PROMPT")
        limit = entry["call_limit"]
        if type(limit) is not int or not 0 <= limit <= 10_000:
            raise ReplayError("CALL_CAP")
        if model is None and limit != 0:
            raise ReplayError("CONTROL_CALL_CAP")
        money(entry["cost_limit_usdt"])
        normalized[name] = dict(entry)
    return normalized


class EvidenceRecorder:
    """New private JSONL evidence journal; exclusive create, fsync per event.

    No resume, replay-based append, network/model invocation, or PAPER broker.
    A later independent verifier must read all evidence, not this live state.
    """

    def __init__(self, path: Path, *, run_id: str, source_class: str,
                 collector_commit: str, registry: dict[str, dict[str, Any]],
                 start_at: datetime, execution_delay_seconds: float = 2.0,
                 proposal_ttl_seconds: float = 120.0,
                 model_calls_authorized: bool = False):
        identity(run_id)
        if source_class not in ("SYNTHETIC", "LIVE_PUBLIC"):
            raise ReplayError("SOURCE_CLASS")
        if not isinstance(collector_commit, str) or not HEX.fullmatch(collector_commit):
            raise ReplayError("SOURCE_COMMIT")
        if type(model_calls_authorized) is not bool:
            raise ReplayError("MODEL_AUTH_FLAG")
        for value in (execution_delay_seconds, proposal_ttl_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ReplayError("BAD_TIMING_LIMIT")
        if not 0.001 <= execution_delay_seconds <= 60 or not 1 <= proposal_ttl_seconds <= 3600:
            raise ReplayError("BAD_TIMING_LIMIT")
        registry = policy_registry(registry)
        path = Path(path).resolve()
        repo = Path(__file__).resolve().parents[2]
        if path == repo or repo in path.parents:
            raise ReplayError("PRIVATE_LEDGER_OUTSIDE_REPO")
        if not path.parent.is_dir():
            raise ReplayError("PREEXISTING_PRIVATE_PARENT_REQUIRED")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.stream = os.fdopen(fd, "wb")
        self.closed = False
        self.seq, self.tip, self.last_at, self.last_elapsed = 0, ZERO, None, -1.0
        self.inputs: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.decisions: set[str] = set()
        self.spent = {name: Decimal("0") for name in registry}
        self.reserved = {name: Decimal("0") for name in registry}
        self.calls = {name: 0 for name in registry}
        self.registry = registry
        self.model_calls_authorized = model_calls_authorized
        self.execution_delay_seconds = execution_delay_seconds
        self.proposal_ttl_seconds = proposal_ttl_seconds
        self._append("run_started", dict(version=VERSION, run_id=run_id,
            source_class=source_class, collector_commit=collector_commit,
            registry=registry, model_calls_authorized=model_calls_authorized,
            execution_delay_seconds=execution_delay_seconds,
            proposal_ttl_seconds=proposal_ttl_seconds), start_at, 0.0)

    def _append(self, kind: str, payload: dict, at: datetime, elapsed: float) -> None:
        if self.closed:
            raise ReplayError("LEDGER_CLOSED")
        when = instant(stamp(at))
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < self.last_elapsed:
            raise ReplayError("MONOTONIC_REGRESSION")
        if self.last_at is not None and when < self.last_at:
            raise ReplayError("UTC_CLOCK_REGRESSION")
        record = dict(seq=self.seq, previous_hash=self.tip, kind=kind,
                      at=when.isoformat(), elapsed_seconds=elapsed, payload=payload)
        record["hash"] = sha256(record)
        self.stream.write(canonical(record) + b"\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.seq, self.tip, self.last_at, self.last_elapsed = (
            self.seq + 1, record["hash"], when, elapsed)

    def freeze(self, frame: dict, *, source_receipts: dict[str, dict],
               raw_cutoff_seq: int, memory: list[dict], at: datetime, elapsed: float) -> None:
        parsed = Frame.parse(frame)
        identity(parsed.id)
        now = instant(stamp(at))
        if parsed.id in self.inputs or parsed.received_at > now:
            raise ReplayError("FRAME_BACKDATED_OR_DUPLICATE")
        if type(raw_cutoff_seq) is not int or raw_cutoff_seq < 0:
            raise ReplayError("RAW_CUTOFF")
        if set(source_receipts) != set(parsed.quotes):
            raise ReplayError("SOURCE_REFS_UNIVERSE")
        seen = set()
        for symbol, ref in source_receipts.items():
            if not isinstance(ref, dict) or set(ref) != {"seq", "sha256", "received_at"}:
                raise ReplayError("SOURCE_REF_SCHEMA")
            n = ref["seq"]
            if type(n) is not int or n <= 0 or n > raw_cutoff_seq or n in seen:
                raise ReplayError("SOURCE_REF_CUTOFF")
            if not isinstance(ref["sha256"], str) or not HEX.fullmatch(ref["sha256"]):
                raise ReplayError("SOURCE_REF_HASH")
            if instant(ref["received_at"]) > parsed.received_at:
                raise ReplayError("FUTURE_SOURCE_REF")
            seen.add(n)
        if not isinstance(memory, list):
            raise ReplayError("MEMORY_SCHEMA")
        for m in memory:
            if not isinstance(m, dict) or set(m) != {"id", "matured_at"}:
                raise ReplayError("MEMORY_SCHEMA")
            identity(m["id"])
            if instant(m["matured_at"]) > parsed.received_at:
                raise ReplayError("IMMATURE_MEMORY")
        payload = dict(frame=frame, frame_sha256=sha256(frame),
            raw_cutoff_seq=raw_cutoff_seq, source_receipts=source_receipts, memory=memory)
        self._append("input_frozen", payload, at, elapsed)
        self.inputs[parsed.id] = payload

    def request(self, *, request_id: str, frame_id: str, policy_id: str,
                reserve_cost_usdt: str, deadline_seconds: float,
                at: datetime, elapsed: float) -> None:
        identity(request_id)
        if not self.model_calls_authorized or policy_id not in self.registry:
            raise ReplayError("UNAUTHORIZED_MODEL_REQUEST")
        policy = self.registry[policy_id]
        if policy["model"] is None or frame_id not in self.inputs or request_id in self.requests:
            raise ReplayError("INVALID_MODEL_REQUEST")
        if any(row["frame_id"] == frame_id and row["policy_id"] == policy_id
               for row in self.requests.values()):
            raise ReplayError("DUPLICATE_POLICY_FRAME_REQUEST")
        if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= 300:
            raise ReplayError("INVALID_DEADLINE")
        now = instant(stamp(at))
        frame_age = (now - instant(self.inputs[frame_id]["frame"]["received_at"])).total_seconds()
        if not 0 <= frame_age <= MAX_REQUEST_AGE_SECONDS:
            raise ReplayError("STALE_OR_FUTURE_REQUEST_INPUT")
        charge = money(reserve_cost_usdt)
        if self.calls[policy_id] >= policy["call_limit"] or (
            self.spent[policy_id] + self.reserved[policy_id] + charge > money(policy["cost_limit_usdt"])):
            raise ReplayError("MODEL_BUDGET_EXHAUSTED")
        payload = dict(request_id=request_id, frame_id=frame_id, policy_id=policy_id,
            input_hash=self.inputs[frame_id]["frame_sha256"],
            policy_version=policy["version"], model=policy["model"],
            prompt_sha256=policy["prompt_sha256"], reserve_cost_usdt=reserve_cost_usdt,
            deadline_at=(now + timedelta(seconds=deadline_seconds)).isoformat())
        self._append("decision_requested", payload, at, elapsed)
        self.calls[policy_id] += 1
        self.reserved[policy_id] += charge
        self.requests[request_id] = dict(payload, requested_at=now, completed=False)

    def complete(self, *, request_id: str, at: datetime, elapsed: float,
                 actual_cost_usdt: str, response_bytes: bytes | None = None,
                 failure_reason: str | None = None, input_tokens: int = 0,
                 output_tokens: int = 0) -> None:
        if request_id not in self.requests or self.requests[request_id]["completed"]:
            raise ReplayError("UNKNOWN_OR_CLOSED_REQUEST")
        req = self.requests[request_id]
        when = instant(stamp(at))
        if when < req["requested_at"]:
            raise ReplayError("RESPONSE_BEFORE_REQUEST")
        for count in (input_tokens, output_tokens):
            if type(count) is not int or count < 0:
                raise ReplayError("BAD_TOKEN_COUNT")
        if (response_bytes is None) == (failure_reason is None):
            raise ReplayError("EXACTLY_ONE_RESPONSE_OR_FAILURE")
        cost = money(actual_cost_usdt)
        if response_bytes is not None:
            if not isinstance(response_bytes, bytes) or len(response_bytes) > 1_000_000:
                raise ReplayError("INVALID_RESPONSE_BYTES")
            kind = "decision_received"
            payload = dict(request_id=request_id,
                response_base64=base64.b64encode(response_bytes).decode("ascii"),
                response_sha256=hashlib.sha256(response_bytes).hexdigest(),
                actual_cost_usdt=actual_cost_usdt, input_tokens=input_tokens,
                output_tokens=output_tokens)
        else:
            if failure_reason not in ("TIMEOUT", "PROVIDER_ERROR", "UNKNOWN_COMPLETION"):
                raise ReplayError("UNKNOWN_FAILURE")
            kind = "decision_failed"
            payload = dict(request_id=request_id, failure_reason=failure_reason,
                actual_cost_usdt=actual_cost_usdt, input_tokens=input_tokens,
                output_tokens=output_tokens)
        self._append(kind, payload, at, elapsed)
        req["completed"] = True
        req["response_kind"] = kind
        req["response_at"] = when
        req["response_bytes"] = response_bytes
        req["response_sha256"] = payload.get("response_sha256")
        policy = req["policy_id"]
        self.reserved[policy] -= money(req["reserve_cost_usdt"])
        self.spent[policy] += cost

    def decide(self, request_id: str, *, at: datetime, elapsed: float) -> None:
        if request_id not in self.requests or request_id in self.decisions:
            raise ReplayError("UNKNOWN_OR_DUPLICATE_PROPOSAL")
        req = self.requests[request_id]
        if req.get("response_kind") != "decision_received":
            raise ReplayError("PROPOSAL_WITHOUT_MODEL_RESPONSE")
        now = instant(stamp(at))
        if now < req["response_at"]:
            raise ReplayError("PROPOSAL_BEFORE_RESPONSE")
        error = None
        raw = None
        try:
            raw = json.loads(req["response_bytes"].decode("utf-8"))
            Plan.parse(raw, set(self.inputs[req["frame_id"]]["frame"]["quotes"]))
        except (UnicodeDecodeError, ValueError, ReplayError) as exc:
            error = "INVALID_RESPONSE_PLAN"
        if req["response_at"] > instant(req["deadline_at"]):
            error = "LATE_MODEL_RESPONSE"
        elif now > req["response_at"] + timedelta(seconds=self.proposal_ttl_seconds):
            error = "PROPOSAL_EXPIRED"
        if error is None:
            self._append("proposal_validated", dict(request_id=request_id, plan=raw,
                source_frame=req["frame_id"], response_sha256=req["response_sha256"],
                eligible_after=(now + timedelta(seconds=self.execution_delay_seconds)).isoformat(),
                valid_until=(req["response_at"] + timedelta(seconds=self.proposal_ttl_seconds)).isoformat()),
                at, elapsed)
        else:
            self._append("proposal_rejected", dict(request_id=request_id, reason=error), at, elapsed)
        self.decisions.add(request_id)

    def close(self, *, at: datetime, elapsed: float) -> None:
        unresolved = sorted(k for k, v in self.requests.items() if not v["completed"])
        undecided = sorted(k for k, v in self.requests.items()
                           if v.get("response_kind") == "decision_received" and k not in self.decisions)
        self._append("run_stopped", dict(unresolved_requests=unresolved,
            responses_without_proposal=undecided,
            spent_usdt={k: str(v) for k, v in self.spent.items()},
            call_counts=dict(self.calls), evidence_class="SOURCE_DECISION_TIMING_ONLY_NO_ALPHA"), at, elapsed)
        self.stream.close()
        self.closed = True

