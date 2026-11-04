"""Independent prospective decision chronology verifier.

Imports neither the decision producer nor model client/PAPER broker.  A valid
hash chain by itself is never evidence of source authenticity or investment
edge: LIVE_PUBLIC verification also requires the original, independently
auditable Bybit source directory.
"""

from __future__ import annotations

import base64
import binascii
import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class AuditError(ValueError):
    pass


ZERO = "0" * 64
VERSION = "ONE_PROSPECTIVE_DECISIONS_V1"
MAX_REQUEST_AGE_SECONDS = 5.0
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


def require(test: bool, reason: str) -> None:
    if not test:
        raise AuditError(reason)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def when(value: Any) -> datetime:
    require(isinstance(value, str), "TIME_NOT_STRING")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("INVALID_TIME") from exc
    require(d.tzinfo is not None and d.utcoffset() is not None, "NAIVE_TIME")
    return d.astimezone(timezone.utc)


def cost(value: Any) -> Decimal:
    require(isinstance(value, str), "COST_NOT_DECIMAL_STRING")
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise AuditError("BAD_COST") from exc
    require(d.is_finite() and d >= 0 and d.as_tuple().exponent >= -8, "BAD_COST")
    return d


def ident(value: Any) -> bool:
    return isinstance(value, str) and IDENT.fullmatch(value) is not None


def num(value: Any, *, positive: bool = False) -> float:
    require(type(value) in (int, float) and math.isfinite(value), "INVALID_NUMBER")
    require(not positive or value > 0, "NONPOSITIVE_NUMBER")
    return float(value)


def check_frame(frame: Any) -> tuple[str, datetime, set[str]]:
    require(isinstance(frame, dict) and set(frame) == {"id", "received_at", "quotes"},
            "FRAME_SCHEMA")
    require(ident(frame["id"]), "FRAME_ID")
    received = when(frame["received_at"])
    quotes = frame["quotes"]
    require(isinstance(quotes, dict) and 0 < len(quotes) <= 100, "FRAME_UNIVERSE")
    for symbol, row in quotes.items():
        require(isinstance(symbol, str) and symbol.isalnum() and len(symbol) <= 24,
                "SYMBOL_SCHEMA")
        require(isinstance(row, dict) and set(row) ==
                {"bid", "ask", "bid_size", "ask_size", "exchange_at"}, "QUOTE_SCHEMA")
        bid, ask = num(row["bid"], positive=True), num(row["ask"], positive=True)
        num(row["bid_size"], positive=True)
        num(row["ask_size"], positive=True)
        require(bid < ask and when(row["exchange_at"]) <= received,
                "CROSSED_OR_FUTURE_QUOTE")
    return frame["id"], received, set(quotes)


def valid_plan(raw: Any, symbols: set[str]) -> bool:
    if not isinstance(raw, dict) or set(raw) != {
        "action", "symbol", "fraction", "p_up", "thesis", "counter_hypothesis", "invalidation"
    }:
        return False
    if raw["action"] not in ("BUY", "SELL", "HOLD") or raw["symbol"] not in symbols | {"CASH"}:
        return False
    for key in ("fraction", "p_up"):
        if type(raw[key]) not in (int, float) or not math.isfinite(raw[key]) or not 0 <= raw[key] <= 1:
            return False
    if (raw["action"] == "HOLD") != (raw["fraction"] == 0):
        return False
    if raw["action"] != "HOLD" and raw["symbol"] == "CASH":
        return False
    return all(isinstance(raw[k], str) and 1 <= len(raw[k]) <= 2000
               for k in ("thesis", "counter_hypothesis", "invalidation"))


def _source_bound(directory: Path, inputs: dict[str, dict]) -> None:
    """Bind the prospective input against real captured receipts and provenance.

    Full-source integrity remains the job of the independently implemented
    Bybit auditor; this verifier checks its result and every referenced byte.
    """
    from .bybit_audit import audit as bybit_audit, AuditError as SourceError
    try:
        source_result = bybit_audit(directory)
    except (SourceError, KeyError, OSError, ValueError) as exc:
        raise AuditError("INDEPENDENT_SOURCE_AUDIT_FAILED") from exc
    require(source_result.get("status") == "PASS_SOURCE_CONSISTENCY_ONLY",
            "INDEPENDENT_SOURCE_AUDIT_FAILED")
    refs = {}
    for item in inputs.values():
        for ref in item["source_receipts"].values():
            existing = refs.setdefault(ref["seq"], ref)
            require(existing == ref, "CONFLICTING_SOURCE_RECEIPT")
    raw_seen = set()
    with (directory / "raw.jsonl").open("rb") as stream:
        for line in stream:
            row = json.loads(line)
            seq = row.get("seq")
            if seq in refs:
                ref = refs[seq]
                require(row["sha256"] == ref["sha256"] and
                        when(row["received_at"]) == when(ref["received_at"]),
                        "SOURCE_RAW_RECEIPT_MISMATCH")
                raw_seen.add(seq)
    require(raw_seen == set(refs), "SOURCE_RECEIPT_MISSING")
    needed = set(inputs)
    frame_seen = set()
    with (directory / "frames.jsonl").open("rb") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("id") in needed:
                require(digest(row) == digest(inputs[row["id"]]["frame"]),
                        "SOURCE_FRAME_MISMATCH")
                frame_seen.add(row["id"])
    require(frame_seen == needed, "SOURCE_FRAME_MISSING")
    provenance_seen = set()
    with (directory / "events.jsonl").open("rb") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("kind") == "frame_provenance" and row.get("frame_id") in needed:
                inp = inputs[row["frame_id"]]
                require(row["raw_cutoff_seq"] == inp["raw_cutoff_seq"] and
                        row["source_seqs"] == {s: ref["seq"] for s, ref in inp["source_receipts"].items()},
                        "SOURCE_PROVENANCE_MISMATCH")
                provenance_seen.add(row["frame_id"])
    require(provenance_seen == needed, "SOURCE_PROVENANCE_MISSING")


def verify(path: Path, *, source_dir: Path | None = None) -> dict[str, Any]:
    """Independently verify a finished journal (never infer economic alpha)."""
    tip, prior_time, prior_elapsed = ZERO, None, -1.0
    header = None
    inputs: dict[str, dict] = {}
    frames_at: dict[str, datetime] = {}
    last_frame_time = None
    frozen_at: dict[str, datetime] = {}
    requests: dict[str, dict] = {}
    already = set()
    calls, spent, reserved = {}, {}, {}
    counts = {"frames": 0, "requests": 0, "received": 0, "failed": 0,
              "validated": 0, "rejected": 0}
    terminal = False
    with Path(path).open("rb") as stream:
        for seq, line in enumerate(stream):
            require(line.endswith(b"\n"), "PARTIAL_LINE")
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, ValueError) as exc:
                raise AuditError("INVALID_JSONL") from exc
            require(isinstance(row, dict) and set(row) ==
                    {"seq", "previous_hash", "kind", "at", "elapsed_seconds", "payload", "hash"},
                    "JOURNAL_SCHEMA")
            require(encoded(row) + b"\n" == line, "NONCANONICAL_EVENT")
            require(type(row["seq"]) is int and row["seq"] == seq and row["previous_hash"] == tip,
                    "CHAIN_SEQUENCE")
            require(isinstance(row["hash"], str) and row["hash"] == digest({k: v for k, v in row.items() if k != "hash"}),
                    "CHAIN_HASH_MISMATCH")
            tip = row["hash"]
            t = when(row["at"])
            elapsed = num(row["elapsed_seconds"])
            require(elapsed >= prior_elapsed and (prior_time is None or t >= prior_time),
                    "CLOCK_REGRESSION")
            prior_time, prior_elapsed = t, elapsed
            kind, p = row["kind"], row["payload"]
            require(isinstance(p, dict) and not terminal, "EVENT_AFTER_TERMINAL")
            if seq == 0:
                require(kind == "run_started" and p.get("version") == VERSION, "RUN_HEADER")
                require(set(p) == {"version", "run_id", "source_class", "collector_commit", "registry",
                                  "model_calls_authorized", "execution_delay_seconds", "proposal_ttl_seconds"},
                        "RUN_HEADER_SCHEMA")
                require(ident(p["run_id"]) and p["source_class"] in ("SYNTHETIC", "LIVE_PUBLIC") and
                        isinstance(p["collector_commit"], str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", p["collector_commit"]) is not None,
                        "RUN_IDENTITY")
                require(type(p["model_calls_authorized"]) is bool, "MODEL_AUTH_FLAG")
                d, ttl = num(p["execution_delay_seconds"], positive=True), num(p["proposal_ttl_seconds"], positive=True)
                require(0.001 <= d <= 60 and 1 <= ttl <= 3600 and elapsed == 0,
                        "RUN_TIMING")
                policies = p["registry"]
                require(isinstance(policies, dict) and policies and "CASH" in policies,
                        "NO_CASH_CONTROL")
                for key, pol in policies.items():
                    require(ident(key) and isinstance(pol, dict) and set(pol) ==
                            {"version", "model", "prompt_sha256", "call_limit", "cost_limit_usdt"},
                            "POLICY_SCHEMA")
                    require(ident(pol["version"]), "POLICY_VERSION")
                    if pol["model"] is None:
                        require(pol["prompt_sha256"] is None and pol["call_limit"] == 0,
                                "CONTROL_POLICY_FIELDS")
                    else:
                        require(ident(pol["model"]) and isinstance(pol["prompt_sha256"], str) and
                                HEX.fullmatch(pol["prompt_sha256"]) is not None,
                                "MODEL_POLICY_FIELDS")
                    require(type(pol["call_limit"]) is int and 0 <= pol["call_limit"] <= 10000,
                            "CALL_CAP")
                    cost(pol["cost_limit_usdt"])
                header = p
                calls = {k: 0 for k in policies}
                spent = {k: Decimal(0) for k in policies}
                reserved = {k: Decimal(0) for k in policies}
                continue
            require(kind != "run_started", "DUPLICATE_HEADER")
            if kind == "input_frozen":
                require(set(p) == {"frame", "frame_sha256", "raw_cutoff_seq", "source_receipts", "memory"},
                        "INPUT_SCHEMA")
                fid, received, symbols = check_frame(p["frame"])
                require(fid not in inputs and (last_frame_time is None or received > last_frame_time),
                        "DUPLICATE_OR_BACKDATED_FRAME")
                require(received <= t and p["frame_sha256"] == digest(p["frame"]),
                        "FRAME_HASH_OR_AVAILABILITY")
                cutoff = p["raw_cutoff_seq"]
                require(type(cutoff) is int and cutoff >= 0 and isinstance(p["source_receipts"], dict)
                        and set(p["source_receipts"]) == symbols, "SOURCE_CUTOFF_SCHEMA")
                seen = set()
                for symbol, source in p["source_receipts"].items():
                    require(isinstance(source, dict) and set(source) ==
                            {"seq", "sha256", "received_at"}, "SOURCE_RECEIPT_SCHEMA")
                    n = source["seq"]
                    require(type(n) is int and 0 < n <= cutoff and n not in seen,
                            "FUTURE_OR_DUPLICATE_SOURCE")
                    require(isinstance(source["sha256"], str) and HEX.fullmatch(source["sha256"]) is not None
                            and when(source["received_at"]) <= received, "FUTURE_OR_UNHASHED_SOURCE")
                    seen.add(n)
                require(isinstance(p["memory"], list), "MEMORY_SCHEMA")
                for m in p["memory"]:
                    require(isinstance(m, dict) and set(m) == {"id", "matured_at"} and ident(m["id"]) and
                            when(m["matured_at"]) <= received, "IMMATURE_MEMORY")
                inputs[fid], frames_at[fid], frozen_at[fid] = p, received, t
                last_frame_time = received
                counts["frames"] += 1
            elif kind == "decision_requested":
                require(set(p) == {"request_id", "frame_id", "policy_id", "input_hash", "policy_version",
                                  "model", "prompt_sha256", "reserve_cost_usdt", "deadline_at"},
                        "REQUEST_SCHEMA")
                rid, fid, pol = p["request_id"], p["frame_id"], p["policy_id"]
                require(ident(rid) and rid not in requests and fid in inputs and pol in header["registry"],
                        "DUPLICATE_OR_UNKNOWN_REQUEST")
                require(not any(q["frame_id"] == fid and q["policy_id"] == pol for q in requests.values()),
                        "DUPLICATE_POLICY_FRAME")
                registered = header["registry"][pol]
                require(header["model_calls_authorized"] and registered["model"] is not None and
                        (p["policy_version"], p["model"], p["prompt_sha256"]) ==
                        (registered["version"], registered["model"], registered["prompt_sha256"]),
                        "UNAUTHORIZED_OR_CHANGED_POLICY")
                require(p["input_hash"] == inputs[fid]["frame_sha256"] and t >= frozen_at[fid],
                        "REQUEST_FUTURE_INPUT")
                require(0 <= (t - frames_at[fid]).total_seconds() <= MAX_REQUEST_AGE_SECONDS,
                        "STALE_REQUEST_INPUT")
                limit = (when(p["deadline_at"]) - t).total_seconds()
                require(1 <= limit <= 300, "REQUEST_DEADLINE")
                charge = cost(p["reserve_cost_usdt"])
                require(calls[pol] < registered["call_limit"] and
                        spent[pol] + reserved[pol] + charge <= cost(registered["cost_limit_usdt"]),
                        "MODEL_BUDGET_VIOLATION")
                calls[pol] += 1
                reserved[pol] += charge
                requests[rid] = dict(p, requested_at=t, result=None, response_at=None, response=None)
                counts["requests"] += 1
            elif kind in ("decision_received", "decision_failed"):
                expected = ({"request_id", "response_base64", "response_sha256", "actual_cost_usdt", "input_tokens", "output_tokens"}
                            if kind == "decision_received" else
                            {"request_id", "failure_reason", "actual_cost_usdt", "input_tokens", "output_tokens"})
                require(set(p) == expected and p.get("request_id") in requests,
                        "RESPONSE_SCHEMA")
                req = requests[p["request_id"]]
                require(req["result"] is None and t >= req["requested_at"],
                        "DUPLICATE_OR_BACKDATED_RESPONSE")
                require(all(type(p[k]) is int and p[k] >= 0 for k in ("input_tokens", "output_tokens")),
                        "BAD_TOKEN_COUNT")
                charge = cost(p["actual_cost_usdt"])
                pol = req["policy_id"]
                reserved[pol] -= cost(req["reserve_cost_usdt"])
                spent[pol] += charge
                require(spent[pol] <= cost(header["registry"][pol]["cost_limit_usdt"]),
                        "ACTUAL_MODEL_COST_OVER_BUDGET")
                if kind == "decision_received":
                    try:
                        raw = base64.b64decode(p["response_base64"], validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise AuditError("INVALID_RESPONSE_BASE64") from exc
                    require(len(raw) <= 1_000_000 and hashlib.sha256(raw).hexdigest() == p["response_sha256"],
                            "RESPONSE_HASH")
                    req["response"] = raw
                    counts["received"] += 1
                else:
                    require(p["failure_reason"] in ("TIMEOUT", "PROVIDER_ERROR", "UNKNOWN_COMPLETION"),
                            "FAILURE_REASON")
                    counts["failed"] += 1
                req["result"], req["response_at"] = kind, t
            elif kind in ("proposal_validated", "proposal_rejected"):
                expected = ({"request_id", "plan", "source_frame", "response_sha256", "eligible_after", "valid_until"}
                            if kind == "proposal_validated" else {"request_id", "reason"})
                require(set(p) == expected and p.get("request_id") in requests,
                        "PROPOSAL_SCHEMA")
                req = requests[p["request_id"]]
                require(req["result"] == "decision_received" and p["request_id"] not in already and
                        t >= req["response_at"], "PROPOSAL_BEFORE_OR_WITHOUT_RESPONSE")
                parsed = None
                try:
                    parsed = json.loads(req["response"].decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    pass
                is_valid = valid_plan(parsed, set(inputs[req["frame_id"]]["frame"]["quotes"]))
                late = req["response_at"] > when(req["deadline_at"])
                expired = t > req["response_at"] + timedelta(seconds=header["proposal_ttl_seconds"])
                if kind == "proposal_validated":
                    require(is_valid and not late and not expired and parsed == p["plan"],
                            "INVALID_OR_LATE_PLAN_APPROVED")
                    require(p["source_frame"] == req["frame_id"] and
                            p["response_sha256"] == hashlib.sha256(req["response"]).hexdigest() and
                            when(p["eligible_after"]) == t + timedelta(
                                seconds=header["execution_delay_seconds"]) and
                            when(p["valid_until"]) == req["response_at"] + timedelta(
                                seconds=header["proposal_ttl_seconds"]),
                            "PROPOSAL_TIMING_FORGED")
                    counts["validated"] += 1
                else:
                    reason = ("LATE_MODEL_RESPONSE" if late else
                              "PROPOSAL_EXPIRED" if expired else "INVALID_RESPONSE_PLAN")
                    require((not is_valid or late or expired) and p["reason"] == reason,
                            "FALSE_PROPOSAL_REJECTION")
                    counts["rejected"] += 1
                already.add(p["request_id"])
            elif kind == "run_stopped":
                require(set(p) == {"unresolved_requests", "responses_without_proposal",
                                  "spent_usdt", "call_counts", "evidence_class"},
                        "TERMINAL_SCHEMA")
                require(p["evidence_class"] == "SOURCE_DECISION_TIMING_ONLY_NO_ALPHA", "FALSE_ALPHA_CLAIM")
                require(p["unresolved_requests"] == sorted(k for k, q in requests.items() if q["result"] is None)
                        and p["responses_without_proposal"] == sorted(k for k, q in requests.items()
                            if q["result"] == "decision_received" and k not in already),
                        "HIDDEN_UNRESOLVED_MODEL")
                require(p["call_counts"] == calls and set(p["spent_usdt"]) == set(spent) and
                        all(cost(p["spent_usdt"][k]) == v for k, v in spent.items()),
                        "HIDDEN_MODEL_COST")
                terminal = True
            else:
                raise AuditError("UNKNOWN_EVENT_KIND")
    require(header is not None and terminal, "MISSING_TERMINAL")
    if header["source_class"] == "LIVE_PUBLIC":
        require(source_dir is not None, "LIVE_SOURCE_FILES_REQUIRED")
        _source_bound(Path(source_dir), inputs)
        status = "PASS_BOUND_SOURCE_TIMING_ONLY"
    else:
        status = "PASS_SYNTHETIC_TIMING_ONLY"
    return {"status": status, "run_id": header["run_id"], "events": seq + 1,
            "frames": len(inputs), "counts": counts, "model_cost_usdt":
            {k: str(v) for k, v in spent.items()}, "tip_sha256": tip,
            "unresolved_requests": len([v for v in requests.values() if v["result"] is None]),
            "paper_fills": "NOT_TESTED", "economic_alpha": "NOT_TESTED"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path,
                        help="Mandatory for LIVE_PUBLIC; complete original Bybit capture directory")
    args = parser.parse_args(argv)
    try:
        report = verify(args.ledger, source_dir=args.source_dir)
    except (AuditError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

