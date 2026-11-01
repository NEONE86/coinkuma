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


def apply_raw_snapshot(payload: bytes, record: dict, state: dict, clocks: dict) -> None:
    """Independently reconstruct book validity from *every* original receipt.

    Invalid symbol-scoped messages invalidate that symbol. Unidentifiable
    payloads invalidate all quotes. Unknown control topics do not mutate books.
    """
    try:
        obj = json.loads(payload)
    except (UnicodeError, ValueError):
        state.clear()
        return
    if not isinstance(obj, dict):
        state.clear()
        return
    if obj.get("op") == "subscribe" and obj.get("success") is False:
        state.clear()
        return
    topic = obj.get("topic")
    if topic not in TOPICS:
        return
    symbol = topic.removeprefix("orderbook.1.")
    try:
        data = obj["data"]
        require(obj["type"] == "snapshot" and data["s"] == symbol, "BAD_SNAPSHOT")
        cts, ts = obj["cts"], obj["ts"]
        require(type(cts) is int and cts >= 0, "BAD_CTS")
        require(type(ts) is int and ts >= 0 or
                type(ts) is str and ts.isascii() and ts.isdigit(), "BAD_TS")
        require(type(data["u"]) is int and data["u"] >= 0 and
                type(data["seq"]) is int and data["seq"] >= 0, "BAD_ORDER")
        earlier = clocks.get(symbol)
        if earlier is not None:
            old_seq, old_cts = earlier
            require(cts >= old_cts and (data["seq"] >= old_seq or data["u"] == 1),
                    "SOURCE_REGRESSION")
        bids, asks = data["b"], data["a"]
        require(len(bids) == len(asks) == 1 and
                len(bids[0]) == len(asks[0]) == 2, "BAD_DEPTH")
        bid, bsize = map(numeric, bids[0])
        ask, asize = map(numeric, asks[0])
        require(bid < ask, "BAD_SPREAD")
        source_at = datetime.fromtimestamp(cts / 1000, timezone.utc)
        require(source_at <= utc(record["received_at"]), "SOURCE_RECEIPT_FUTURE")
    except (KeyError, IndexError, ValueError, TypeError, OverflowError, OSError):
        state.pop(symbol, None)
        return
    state[symbol] = record["seq"]
    clocks[symbol] = (data["seq"], cts)


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
    terminal_at = utc(terminal["at"])
    terminal_tick = terminal.get("elapsed_seconds")
    require(type(terminal_tick) in (float, int) and math.isfinite(terminal_tick) and
            terminal_tick >= 0 and terminal_at >= first_at, "TERMINAL_CHRONOLOGY")
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
    resets = [e for e in events if e.get("kind") == "connection_reset"]
    require(resets, "CONNECTION_GENERATION_MISSING")
    reset_lookup = {}
    last_reset_tick, last_reset_wall = 0.0, first_at
    for index, reset in enumerate(resets, 1):
        elapsed = reset.get("elapsed_seconds")
        at = utc(reset["at"])
        require(reset.get("generation") == index and
                type(elapsed) in (float, int) and math.isfinite(elapsed) and
                last_reset_tick <= elapsed <= terminal_tick and
                last_reset_wall <= at <= terminal_at, "CONNECTION_GENERATION_ORDER")
        reset_lookup[index] = (at, elapsed)
        last_reset_tick, last_reset_wall = elapsed, at

    # The events file is append-only in actual processing order. In particular,
    # a reset may have a monotonic time earlier than a sampled frame but a
    # fabricated UTC later than that frame. Checking the two clocks with an
    # AND condition would hide that impossible ordering. Check BOTH clocks
    # individually across the full ordered reset/frame event trace.
    event_wall, event_tick = first_at, 0.0
    active_generation, frame_cursor = 0, 0
    for event in events[1:-1]:
        kind = event.get("kind")
        if kind == "connection_reset":
            active_generation = event["generation"]
            at, tick = utc(event["at"]), event["elapsed_seconds"]
        elif kind == "frame_provenance":
            require(frame_cursor < len(frames) and
                    event.get("frame_id") == frames[frame_cursor].get("id"),
                    "EVENT_FRAME_ORDER")
            at = utc(frames[frame_cursor]["received_at"])
            tick = event["sample_elapsed_seconds"]
            frame_cursor += 1
        else:
            continue
        require(tick >= event_tick and at >= event_wall,
                "RESET_SAMPLE_CLOCK_CONTRADICTION")
        if kind == "frame_provenance":
            require(event.get("generation") == active_generation,
                    "FRAME_RECONNECT_INVALIDATION")
        event_tick, event_wall = tick, at
    require(frame_cursor == len(frames) and
            event_tick <= terminal_tick and event_wall <= terminal_at,
            "EVENT_TERMINAL_ORDER")

    wanted: set[int] = set()
    cutoff_frames: dict[int, list[int]] = {}
    expected_states: dict[int, dict[str, int]] = {}
    successor_ids: set[int] = set()
    successor_receipts: dict[int, tuple[datetime, float]] = {}
    prior_wall = first_at
    prior_tick = 0.0
    prior_cutoff = 0
    for index, (frame, prov) in enumerate(zip(frames, provenance), 1):
        require(set(frame) == {"id", "received_at", "quotes"}, "FRAME_SCHEMA")
        require(frame["id"] == f"bybit-{index:08d}" == prov.get("frame_id"), "FRAME_ID_MISMATCH")
        require(set(frame["quotes"]) == set(SYMBOLS), "INCOMPLETE_FRAME")
        at = utc(frame["received_at"])
        tick = prov.get("sample_elapsed_seconds")
        require(type(tick) in (float, int) and math.isfinite(tick) and tick >= prior_tick + 1,
                "NON_MONOTONIC_SAMPLE")
        require(at > prior_wall, "NON_MONOTONIC_FRAME_TIME")
        require(tick < duration, "FRAME_AFTER_DURATION")
        require(tick <= terminal_tick and at <= terminal_at, "FRAME_AFTER_TERMINAL")
        require(type(prov.get("generation")) is int and prov["generation"] >= 1,
                "INVALID_FRAME_GENERATION")
        cutoff = prov.get("raw_cutoff_seq")
        require(type(cutoff) is int and prior_cutoff <= cutoff <= terminal["raw_count"] and cutoff >= 1,
                "INVALID_RAW_CUTOFF")
        cutoff_frames.setdefault(cutoff, []).append(index - 1)
        successor_ids.add(cutoff + 1)
        seqs = prov.get("source_seqs")
        require(isinstance(seqs, dict) and set(seqs) == set(SYMBOLS), "INCOMPLETE_SOURCE_PROVENANCE")
        for symbol in SYMBOLS:
            seq = seqs[symbol]
            require(type(seq) is int and 1 <= seq <= cutoff, "INVALID_SOURCE_SEQ")
            wanted.add(seq)
        prior_wall, prior_tick = at, tick
        prior_cutoff = cutoff

    referenced = {}
    total_raw = 0
    raw_wall = first_at
    raw_tick = 0.0
    reconstructed = {}
    source_clocks = {}
    generation = 0
    for record in rows(root / "raw.jsonl"):
        total_raw += 1
        require(record.get("seq") == total_raw, "RAW_SEQUENCE_GAP")
        received = utc(record["received_at"])
        elapsed = record.get("elapsed_seconds")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and
                raw_tick <= elapsed < duration and received >= raw_wall, "RAW_CHRONOLOGY")
        require(elapsed <= terminal_tick and received <= terminal_at, "RAW_AFTER_TERMINAL")
        require(type(record.get("generation")) is int and record["generation"] >= 1,
                "RAW_GENERATION")
        require(record["generation"] >= generation and record["generation"] in reset_lookup,
                "RAW_GENERATION_ORDER")
        reset_at, reset_tick = reset_lookup[record["generation"]]
        require(reset_at <= received and reset_tick <= elapsed, "RAW_BEFORE_CONNECTION")
        if record["generation"] != generation:
            reconstructed.clear()
            source_clocks.clear()
            generation = record["generation"]
        try:
            payload = base64.b64decode(record["payload_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("RAW_BYTES_INVALID") from exc
        require(hashlib.sha256(payload).hexdigest() == record.get("sha256"), "RAW_HASH_MISMATCH")
        apply_raw_snapshot(payload, record, reconstructed, source_clocks)
        if total_raw in wanted:
            try:
                obj = json.loads(payload)
            except (UnicodeError, ValueError) as exc:
                raise AuditError("REFERENCED_RAW_NOT_JSON") from exc
            require(isinstance(obj, dict), "REFERENCED_RAW_NOT_OBJECT")
            referenced[total_raw] = (record, obj)
        for frame_index in cutoff_frames.get(total_raw, ()):
            frame_at = utc(frames[frame_index]["received_at"])
            tick = provenance[frame_index]["sample_elapsed_seconds"]
            require(elapsed <= tick and received <= frame_at, "CUTOFF_INCLUDES_FUTURE_RECEIPT")
            expected_states[frame_index] = dict(reconstructed)
        if total_raw in successor_ids:
            successor_receipts[total_raw] = (received, elapsed)
        raw_wall, raw_tick = received, elapsed
    require(total_raw == terminal["raw_count"], "RAW_COUNT_MISMATCH")
    require(set(referenced) == wanted, "PROVENANCE_REFERENCES_MISSING")
    require(total_raw > 0 and frames, "NO_VALID_MARKET_EVIDENCE")
    require(len(expected_states) == len(frames), "RAW_CUTOFF_MISSING")
    for frame_index, (frame, prov) in enumerate(zip(frames, provenance)):
        require(expected_states[frame_index] == prov["source_seqs"],
                "FRAME_REFERENCES_INVALIDATED_SOURCE")
        next_receipt = successor_receipts.get(prov["raw_cutoff_seq"] + 1)
        if next_receipt is not None:
            next_wall, next_tick = next_receipt
            # Both clocks must be nonregressing across the sample boundary.
            # Equality in either clock is allowed for same-tick processing,
            # but an earlier UTC with later monotonic is still contradictory.
            sample_tick = prov["sample_elapsed_seconds"]
            require(next_tick >= sample_tick and next_wall >= utc(frame["received_at"]),
                    "CUTOFF_OMITS_EARLIER_RECEIPT")

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

    # The live collector has a hard 300s deadline since its last emitted valid
    # book. A forged DURATION terminal cannot claim a full capture after an
    # uninterrupted gap that would have stopped the actual network loop.
    if terminal["reason"] == "DURATION":
        emitted = [p["sample_elapsed_seconds"] for p in provenance]
        intervals = [emitted[0], *[later - earlier for earlier, later in
                                  zip(emitted, emitted[1:])], terminal_tick - emitted[-1]]
        require(all(0 <= gap < 300 for gap in intervals), "NO_VALID_BOOK_DEADLINE_VIOLATION")

    nominal_coverage = len(frames) / max(1, duration - 1)

    return {"status": "PASS_SOURCE_CONSISTENCY_ONLY", "complete_24h":
            terminal["reason"] == "DURATION" and duration == 86400 and nominal_coverage >= 0.90,
            "terminal_reason": terminal["reason"], "raw_count": total_raw,
            "frame_count": len(frames), "missed_count": terminal["missed_count"],
            "nominal_sample_coverage": round(nominal_coverage, 6),
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
