"""Read-only, strategy-independent audit of a Bybit forward source capture.

This module intentionally does not import the capture implementation, the PAPER
engine or any model. A successful audit proves internal source/frame consistency,
not exchange authenticity, contemporaneous AI inference, fills or profitability.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TOPICS = tuple(f"orderbook.1.{s}" for s in SYMBOLS)


class AuditError(ValueError):
    """Inconsistent, incomplete or unverifiable source evidence."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AuditError(reason)


def utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
        require(dt.utcoffset() == timedelta(0), "TIMESTAMP_NOT_UTC")
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuditError("INVALID_TIMESTAMP") from exc


def rows(path: Path):
    try:
        with path.open("rb") as stream:
            for n, raw in enumerate(stream, 1):
                require(raw.endswith(b"\n"), f"UNTERMINATED_LINE:{path.name}:{n}")
                try:
                    obj = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    raise AuditError(f"INVALID_JSON:{path.name}:{n}") from exc
                require(isinstance(obj, dict), f"INVALID_ROW:{path.name}:{n}")
                yield obj
    except OSError as exc:
        raise AuditError(f"UNREADABLE:{path.name}") from exc


def numeric(value) -> float:
    try:
        require(not isinstance(value, bool), "INVALID_BOOK_NUMBER")
        out = float(value)
        require(math.isfinite(out) and out > 0, "INVALID_BOOK_NUMBER")
        return out
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditError("INVALID_BOOK_NUMBER") from exc


def audit(path: Path) -> dict:
    root = Path(path)
    names = ("raw.jsonl", "frames.jsonl", "events.jsonl")
    require(root.is_dir() and all((root / n).is_file() for n in names), "CAPTURE_FILES_MISSING")
    events = list(rows(root / "events.jsonl"))
    frames = list(rows(root / "frames.jsonl"))
    require(len(events) >= 2 and events[0].get("kind") == "start", "START_MISSING")
    require(events[-1].get("kind") == "terminal", "TERMINAL_MISSING")
    start, terminal = events[0], events[-1]
    require(tuple(start.get("topics", ())) == TOPICS, "TOPIC_SCOPE_MISMATCH")
    require(start.get("endpoint") == "wss://stream.bybit.com/v5/public/spot", "ENDPOINT_MISMATCH")
    require(start.get("model_calls") == 0 and start.get("real_orders") == 0, "SCOPE_MISMATCH")
    require(start.get("sample_interval_seconds") == 1, "INTERVAL_MISMATCH")
    require(start.get("freshness_seconds_exclusive") == 5, "FRESHNESS_MISMATCH")
    maximum, duration = start.get("max_bytes"), start.get("duration_seconds")
    require(type(maximum) is int and 5120 <= maximum <= 1_000_000_000, "INVALID_MAX_BYTES")
    require(type(duration) is int and 1 <= duration <= 86400, "INVALID_DURATION")
    first_at = utc(start["started_at"])
    require(type(terminal.get("frame_count")) is int and terminal["frame_count"] == len(frames),
            "FRAME_COUNT_MISMATCH")
    require(len(frames) <= duration, "TOO_MANY_FRAMES")
    require(type(terminal.get("raw_count")) is int, "RAW_COUNT_MISSING")
    byte_count = sum((root / n).stat().st_size for n in names)
    require(byte_count <= maximum, "STORAGE_CAP_EXCEEDED")
    term_line = (json.dumps(terminal, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    require(terminal.get("bytes_before_terminal") == byte_count - len(term_line), "BYTE_ACCOUNTING_MISMATCH")
    if terminal.get("reason") == "DURATION":
        require(terminal.get("elapsed_seconds", -1) >= duration, "PREMATURE_DURATION_CLAIM")

    provenance = [e for e in events if e.get("kind") == "frame_provenance"]
    require(len(provenance) == len(frames), "PROVENANCE_COUNT_MISMATCH")
    require(terminal.get("missed_count") ==
            sum(e.get("count", 0) for e in events if e.get("kind") == "missed_ticks") +
            sum(e.get("kind") == "missed_frame" for e in events), "MISS_COUNT_MISMATCH")

    wanted: set[int] = set()
    prior_wall = first_at
    prior_tick = 0.0
    for index, (frame, prov) in enumerate(zip(frames, provenance), 1):
        require(set(frame) == {"id", "received_at", "quotes"}, "FRAME_SCHEMA")
        require(frame["id"] == f"bybit-{index:08d}" == prov.get("frame_id"), "FRAME_ID_MISMATCH")
        require(set(frame["quotes"]) == set(SYMBOLS), "INCOMPLETE_FRAME")
        at = utc(frame["received_at"])
        tick = prov.get("sample_elapsed_seconds")
        require(type(tick) in (float, int) and math.isfinite(tick) and tick >= prior_tick + 1,
                "NON_MONOTONIC_SAMPLE")
        require(at > prior_wall, "NON_MONOTONIC_FRAME_TIME")
        require(type(prov.get("generation")) is int and prov["generation"] >= 1,
                "INVALID_FRAME_GENERATION")
        seqs = prov.get("source_seqs")
        require(isinstance(seqs, dict) and set(seqs) == set(SYMBOLS), "INCOMPLETE_SOURCE_PROVENANCE")
        for symbol in SYMBOLS:
            seq = seqs[symbol]
            require(type(seq) is int and 1 <= seq <= terminal["raw_count"], "INVALID_SOURCE_SEQ")
            wanted.add(seq)
        prior_wall, prior_tick = at, tick

    referenced = {}
    total_raw = 0
    raw_wall = first_at
    raw_tick = 0.0
    for record in rows(root / "raw.jsonl"):
        total_raw += 1
        require(record.get("seq") == total_raw, "RAW_SEQUENCE_GAP")
        received = utc(record["received_at"])
        elapsed = record.get("elapsed_seconds")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and
                raw_tick <= elapsed < duration and received >= raw_wall, "RAW_CHRONOLOGY")
        require(type(record.get("generation")) is int and record["generation"] >= 1,
                "RAW_GENERATION")
        try:
            payload = base64.b64decode(record["payload_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("RAW_BYTES_INVALID") from exc
        require(hashlib.sha256(payload).hexdigest() == record.get("sha256"), "RAW_HASH_MISMATCH")
        if total_raw in wanted:
            try:
                obj = json.loads(payload)
            except (UnicodeError, ValueError) as exc:
                raise AuditError("REFERENCED_RAW_NOT_JSON") from exc
            require(isinstance(obj, dict), "REFERENCED_RAW_NOT_OBJECT")
            referenced[total_raw] = (record, obj)
        raw_wall, raw_tick = received, elapsed
    require(total_raw == terminal["raw_count"], "RAW_COUNT_MISMATCH")
    require(set(referenced) == wanted, "PROVENANCE_REFERENCES_MISSING")
    require(total_raw > 0 and frames, "NO_VALID_MARKET_EVIDENCE")

    last_by_symbol = {}
    for frame, prov in zip(frames, provenance):
        frame_at = utc(frame["received_at"])
        tick = prov["sample_elapsed_seconds"]
        for symbol in SYMBOLS:
            receipt, obj = referenced[prov["source_seqs"][symbol]]
            source_time = utc(receipt["received_at"])
            require(receipt["generation"] == prov["generation"], "CROSS_GENERATION_QUOTE")
            require(0 <= tick - receipt["elapsed_seconds"] < 5 and
                    0 <= (frame_at - source_time).total_seconds() < 5,
                    "SOURCE_RECEIVED_AFTER_SAMPLE_OR_STALE")
            require(obj.get("topic") == f"orderbook.1.{symbol}" and obj.get("type") == "snapshot",
                    "REFERENCED_TOPIC_MISMATCH")
            data = obj.get("data")
            require(isinstance(data, dict) and data.get("s") == symbol, "REFERENCED_SYMBOL_MISMATCH")
            require(type(data.get("seq")) is int and type(data.get("u")) is int,
                    "REFERENCED_ORDER_MISSING")
            require(type(obj.get("cts")) is int and obj["cts"] >= 0, "REFERENCED_CTS_INVALID")
            exchange = datetime.fromtimestamp(obj["cts"] / 1000, tz=timezone.utc)
            require(0 <= (frame_at - exchange).total_seconds() < 5, "EXCHANGE_FUTURE_OR_STALE")
            previous = last_by_symbol.get(symbol)
            if previous is not None and previous[0] == prov["generation"]:
                _, old_seq, old_cts = previous
                require(obj["cts"] >= old_cts, "REFERENCED_CTS_REGRESSION")
                if data["seq"] < old_seq:
                    require(data["u"] == 1, "REFERENCED_SEQ_REGRESSION")
                    require(any(e.get("kind") == "source_restart" and
                                e.get("raw_seq") == receipt["seq"] and
                                e.get("symbol") == symbol for e in events), "UNLOGGED_SOURCE_RESTART")
            last_by_symbol[symbol] = (prov["generation"], data["seq"], obj["cts"])
            quote = frame["quotes"][symbol]
            try:
                bid, bsize = data["b"][0]
                ask, asize = data["a"][0]
                expected = (numeric(bid), numeric(ask), numeric(bsize), numeric(asize))
                actual = tuple(numeric(quote[k]) for k in ("bid", "ask", "bid_size", "ask_size"))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise AuditError("REFERENCED_BOOK_INVALID") from exc
            require(expected[0] < expected[1] and actual == expected, "FRAME_RAW_QUOTE_MISMATCH")
            require(utc(quote["exchange_at"]) == exchange, "FRAME_RAW_CTS_MISMATCH")

    return {"status": "PASS_SOURCE_CONSISTENCY_ONLY", "complete_24h":
            terminal["reason"] == "DURATION" and duration == 86400,
            "terminal_reason": terminal["reason"], "raw_count": total_raw,
            "frame_count": len(frames), "missed_count": terminal["missed_count"],
            "bytes": byte_count, "model_decisions": 0, "economic_alpha": "NOT_TESTED"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(audit(args.capture_dir), sort_keys=True))
        return 0
    except (AuditError, KeyError, OverflowError, OSError) as exc:
        print(json.dumps({"status": "FAIL_SOURCE_AUDIT", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
