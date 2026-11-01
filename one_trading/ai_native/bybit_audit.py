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


def apply_raw_snapshot(payload: bytes, record: dict, state: dict, clocks: dict):
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
    if earlier is not None and data["seq"] < earlier[0]:
        return dict(raw_seq=record["seq"], symbol=symbol,
                    generation=record["generation"], at=record["received_at"])


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
    cleanup_tick = terminal_tick
    fatal_stop = terminal.get("fatal_stop")
    if fatal_stop is not None:
        require(isinstance(fatal_stop, dict) and fatal_stop.get("reason") == "STORAGE_QUOTA"
                and terminal.get("reason") == "STORAGE_QUOTA", "FATAL_STOP_REASON")
        tick, at = fatal_stop.get("elapsed_seconds"), utc(fatal_stop.get("at"))
        require(type(tick) in (int, float) and math.isfinite(tick) and 0 <= tick <= terminal_tick
                and first_at <= at <= terminal_at, "FATAL_STOP_CHRONOLOGY")
        require(all(type(fatal_stop.get(k)) is int and fatal_stop[k] == terminal.get(k)
                    for k in ("raw_count", "frame_count", "sample_attempts")), "FATAL_STOP_COUNTS")
        used, attempted = fatal_stop.get("used_bytes"), fatal_stop.get("attempted_bytes")
        require(type(used) is int and used == terminal["bytes_before_terminal"] and used <= maximum-4096
                and type(attempted) is int and attempted > 0 and used + attempted > maximum-4096,
                "FATAL_STOP_QUOTA_EVIDENCE")
        # All collection events must precede the recorded fatal boundary;
        # only cleanup/terminal serialization may happen afterwards.
        terminal_tick, terminal_at = tick, at
    if terminal.get("reason") == "DURATION":
        require(terminal.get("elapsed_seconds", -1) >= duration, "PREMATURE_DURATION_CLAIM")

    provenance = [e for e in events if e.get("kind") == "frame_provenance"]
    missed_samples = [e for e in events if e.get("kind") == "missed_frame"]
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
        elif kind == "missed_frame":
            require("sample_elapsed_seconds" in event, "SAMPLING_EVIDENCE_REQUIRED")
            at, tick = utc(event["at"]), event["sample_elapsed_seconds"]
        else:
            continue
        require(tick >= event_tick and at >= event_wall,
                "RESET_SAMPLE_CLOCK_CONTRADICTION")
        if kind in ("frame_provenance", "missed_frame"):
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
    subscription_rejection = None
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
        require(tick - prior_tick < 300, "NO_VALID_BOOK_DEADLINE_VIOLATION")
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
    replayed_restarts = []
    missed_cutoffs = {}
    for sample in missed_samples:
        cutoff = sample.get("raw_cutoff_seq")
        require(type(cutoff) is int and 0 <= cutoff <= terminal["raw_count"], "SAMPLING_RAW_CUTOFF")
        missed_cutoffs.setdefault(cutoff, []).append(sample)
        successor_ids.add(cutoff + 1)
    book_receipts = {}
    def check_missed(cutoff, received, elapsed):
        for sample in missed_cutoffs.get(cutoff, ()):
            at, tick = utc(sample["at"]), sample["sample_elapsed_seconds"]
            require(received <= at and elapsed <= tick, "SAMPLING_FUTURE_CUTOFF")
            missing = []
            for symbol in SYMBOLS:
                if symbol not in reconstructed or sample["generation"] != generation:
                    missing.append(symbol)
                    continue
                wall, mono = book_receipts[symbol]
                exchange = datetime.fromtimestamp(source_clocks[symbol][1] / 1000, timezone.utc)
                if not (0 <= tick-mono < 5 and 0 <= (at-wall).total_seconds() < 5
                        and 0 <= (at-exchange).total_seconds() < 5):
                    missing.append(symbol)
            require(missing and sample.get("symbols") == missing, "SAMPLING_FALSE_MISSING_BOOK")
    check_missed(0, first_at, 0)
    emitted_cursor, last_emitted_tick = 0, 0.0
    for record in rows(root / "raw.jsonl"):
        # A rejected subscription is terminal for Capture.ingest()/network().
        # Clearing books alone is insufficient: a forged stream could present
        # fresh snapshots and a successful completion after a mandatory stop.
        require(subscription_rejection is None, "SUBSCRIPTION_CONTINUED_AFTER_REJECTION")
        total_raw += 1
        require(record.get("seq") == total_raw, "RAW_SEQUENCE_GAP")
        received = utc(record["received_at"])
        elapsed = record.get("elapsed_seconds")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and
                raw_tick <= elapsed < duration and received >= raw_wall, "RAW_CHRONOLOGY")
        require(elapsed <= terminal_tick and received <= terminal_at, "RAW_AFTER_TERMINAL")
        while emitted_cursor < len(provenance) and provenance[emitted_cursor]["raw_cutoff_seq"] < total_raw:
            last_emitted_tick = provenance[emitted_cursor]["sample_elapsed_seconds"]
            emitted_cursor += 1
        require(elapsed - last_emitted_tick < 300, "NO_VALID_BOOK_DEADLINE_RAW")
        require(type(record.get("generation")) is int and record["generation"] >= 1,
                "RAW_GENERATION")
        require(record["generation"] >= generation and record["generation"] in reset_lookup,
                "RAW_GENERATION_ORDER")
        reset_at, reset_tick = reset_lookup[record["generation"]]
        require(reset_at <= received and reset_tick <= elapsed, "RAW_BEFORE_CONNECTION")
        following = reset_lookup.get(record["generation"] + 1)
        if following is not None:
            require(received <= following[0] and elapsed <= following[1], "RAW_AFTER_CONNECTION")
        if record["generation"] != generation:
            reconstructed.clear()
            source_clocks.clear()
            book_receipts.clear()
            generation = record["generation"]
        try:
            payload = base64.b64decode(record["payload_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("RAW_BYTES_INVALID") from exc
        require(hashlib.sha256(payload).hexdigest() == record.get("sha256"), "RAW_HASH_MISMATCH")
        try:
            control = json.loads(payload)
        except (UnicodeError, ValueError):
            control = None
        if (isinstance(control, dict) and control.get("op") == "subscribe" and
                control.get("success") is False):
            subscription_rejection = (total_raw, received, elapsed, generation)
        restart = apply_raw_snapshot(payload, record, reconstructed, source_clocks)
        if restart is not None:
            replayed_restarts.append(restart)
        for symbol, seq in reconstructed.items():
            if seq == total_raw:
                book_receipts[symbol] = (received, elapsed)
        check_missed(total_raw, received, elapsed)
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
    restart_events = [{k: e.get(k) for k in ("raw_seq", "symbol", "generation", "at")}
                      for e in events if e.get("kind") == "source_restart"]
    require(restart_events == replayed_restarts, "SOURCE_RESTART_EVIDENCE_MISMATCH")
    event_cutoff, event_generation = 0, 0
    pending_restarts = []
    for event in events[1:-1]:
        if event.get("kind") == "connection_reset":
            event_generation = event["generation"]
        elif event.get("kind") == "source_restart":
            require(event["raw_seq"] > event_cutoff and event["generation"] == event_generation,
                    "SOURCE_RESTART_EVENT_ORDER")
            pending_restarts.append(event["raw_seq"])
        elif event.get("kind") in ("frame_provenance", "missed_frame"):
            event_cutoff = event["raw_cutoff_seq"]
            require(all(seq <= event_cutoff for seq in pending_restarts), "SOURCE_RESTART_EVENT_ORDER")
            pending_restarts.clear()
    subscription_events = [e for e in events if e.get("kind") == "subscription_failure"]
    if subscription_rejection is None:
        require(not subscription_events and terminal.get("reason") != "SUBSCRIPTION_REJECTED",
                "SUBSCRIPTION_TERMINAL_MISMATCH")
    else:
        rejected_seq, rejected_at, rejected_tick, rejected_generation = subscription_rejection
        require(terminal.get("reason") == "SUBSCRIPTION_REJECTED" and
                len(subscription_events) == 1 and events[-2] is subscription_events[0],
                "SUBSCRIPTION_TERMINAL_MISMATCH")
        failure = subscription_events[0]
        require(failure.get("raw_seq") == rejected_seq and
                failure.get("generation") == rejected_generation and
                utc(failure["at"]) == rejected_at and
                all(p["raw_cutoff_seq"] < rejected_seq for p in provenance) and
                (not provenance or (provenance[-1]["sample_elapsed_seconds"] <= rejected_tick and
                                   utc(frames[-1]["received_at"]) <= rejected_at)),
                "SUBSCRIPTION_FAILURE_PROVENANCE_MISMATCH")
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

    # Reconstruct the actual sampler state; counts in two mutable summaries
    # are not independent evidence. Older missed-frame records lack a clock
    # and are explicitly unverifiable rather than assigned an invented time.
    next_tick, last_valid, attempts, misses = 1.0, 0.0, 0, 0
    pending_skip = None
    prior_cutoff = 0
    sample_version = start.get("sampling_evidence_version", 1)
    require(type(sample_version) is int and sample_version in (1, 2), "SAMPLING_VERSION")
    for event in events[1:-1]:
        kind = event.get("kind")
        if kind == "missed_ticks":
            require(pending_skip is None, "SAMPLING_DUPLICATE_SKIP")
            pending_skip = event
            continue
        if kind not in ("frame_provenance", "missed_frame"):
            require(pending_skip is None, "SAMPLING_UNBOUND_SKIP")
            continue
        tick = event.get("sample_elapsed_seconds")
        require(type(tick) in (int, float) and math.isfinite(tick) and tick >= next_tick,
                "SAMPLING_ATTEMPT_ORDER")
        require(tick < duration and tick <= terminal_tick, "SAMPLING_AFTER_TERMINAL")
        require(tick-last_valid < 300, "NO_VALID_BOOK_DEADLINE_SAMPLE")
        cutoff = event["raw_cutoff_seq"]
        require(cutoff >= prior_cutoff, "SAMPLING_CUTOFF_ORDER")
        prior_cutoff = cutoff
        attempts += 1
        if sample_version == 2:
            require(type(event.get("sample_index")) is int and event["sample_index"] == attempts,
                    "SAMPLING_INDEX")
        skipped = int(tick-next_tick)
        require((pending_skip is not None) == (skipped > 0), "SAMPLING_MISSED_TICKS_MISSING")
        sample_at = utc(event["at"]) if kind == "missed_frame" else utc(frames[int(event["frame_id"].split("-")[1])-1]["received_at"])
        if pending_skip is not None:
            require(type(pending_skip.get("count")) is int and pending_skip["count"] == skipped
                    and utc(pending_skip["at"]) == sample_at, "SAMPLING_MISSED_TICKS_COUNT")
            if sample_version == 2:
                require(all(pending_skip.get(k) == event.get(k) for k in
                            ("sample_index", "sample_elapsed_seconds", "raw_cutoff_seq", "generation")),
                        "SAMPLING_SKIP_PROVENANCE")
        if kind == "missed_frame":
            successor = successor_receipts.get(cutoff + 1)
            if successor is not None:
                require(successor[0] >= sample_at and successor[1] >= tick,
                        "SAMPLING_CUTOFF_OMITS_EARLIER_RECEIPT")
        else:
            last_valid = tick
        misses += skipped + (kind == "missed_frame")
        pending_skip = None
        next_tick = tick + 1
    require(pending_skip is None, "SAMPLING_UNBOUND_SKIP")
    deadline = last_valid + 300
    if terminal["reason"] == "NO_VALID_BOOK":
        require(deadline < duration or not frames, "NO_VALID_BOOK_TERMINAL_PRIORITY")
        require(terminal_tick >= min(deadline, duration), "PREMATURE_NO_VALID_BOOK")
        require(terminal_tick >= deadline or not frames, "PREMATURE_NO_VALID_BOOK")
    elif terminal["reason"] == "DURATION":
        require(duration <= deadline, "NO_VALID_BOOK_DEADLINE_VIOLATION")
    elif subscription_rejection is None:
        require(terminal_tick <= deadline, "NO_VALID_BOOK_DEADLINE_TERMINAL")

    unsampled = max(0, math.ceil(min(terminal_tick, duration)-next_tick))
    require(type(terminal.get("missed_count")) is int and terminal["missed_count"] == misses,
            "SAMPLING_MISSED_COUNT")
    require(type(terminal.get("unsampled_due_ticks")) is int and
            terminal["unsampled_due_ticks"] == unsampled, "SAMPLING_UNSAMPLED_COUNT")
    if sample_version == 2:
        require(type(terminal.get("sample_attempts")) is int and terminal["sample_attempts"] == attempts,
                "SAMPLING_ATTEMPT_COUNT")

    nominal_coverage = len(frames) / max(1, duration - 1)

    return {"status": "PASS_SOURCE_CONSISTENCY_ONLY", "complete_24h":
            terminal["reason"] == "DURATION" and duration == 86400 and nominal_coverage >= 0.90,
            "terminal_reason": terminal["reason"], "raw_count": total_raw,
            "frame_count": len(frames), "missed_count": terminal["missed_count"],
            "sampling_evidence_version": sample_version, "sample_attempts": attempts,
            "missed_frame_count": len(missed_samples), "missed_ticks": misses-len(missed_samples),
            "unsampled_due_ticks": unsampled,
            "collection_stop_elapsed_seconds": terminal_tick, "cleanup_elapsed_seconds": cleanup_tick,
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
