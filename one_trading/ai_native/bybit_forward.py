"""Bounded public Bybit L1 capture, isolated from all trading/model code.

The adapter can be driven with recorded bytes and explicit clocks in offline tests.
It emits input packets only; it never calls the PAPER engine or executes orders.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .engine import Frame, ReplayError

ENDPOINT = "wss://stream.bybit.com/v5/public/spot"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TOPICS = tuple("orderbook.1." + s for s in SYMBOLS)
MAX_BYTES = 1_000_000_000
MAX_DURATION = 86_400
TERMINAL_RESERVE = 4096


class StopCapture(Exception):
    pass


def stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC timestamp required")
    return value.astimezone(timezone.utc).isoformat()


def line(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


class Capture:
    """Append-only sink plus deterministic source-record and sampling adapter."""
    def __init__(self, output: Path, start_at: datetime, start_mono: float,
                 duration: int = MAX_DURATION, max_bytes: int = MAX_BYTES):
        if not 1 <= duration <= MAX_DURATION or not TERMINAL_RESERVE + 1024 <= max_bytes <= MAX_BYTES:
            raise ValueError("invalid capture bounds")
        if not math.isfinite(start_mono):
            raise ValueError("invalid monotonic clock")
        start = stamp(start_at)
        output = Path(output).absolute()
        repo = Path(__file__).resolve().parents[2]
        if output.resolve().is_relative_to(repo):
            raise ValueError("output must be outside repository")
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.files = {name: (output / (name + ".jsonl")).open("xb")
                      for name in ("raw", "frames", "events")}
        for stream in self.files.values():
            os.chmod(stream.name, 0o600)
        self.used = 0
        self.limit, self.duration = max_bytes, duration
        self.start_mono = self.last_mono = start_mono
        self.last_wall = start_at.astimezone(timezone.utc)
        self.next_tick = start_mono + 1
        self.quotes = {}
        self.source_clocks = {}
        self.seq = self.frames = self.missed = self.generation = 0
        self.last_frame_at = None
        self.last_frame_mono = start_mono
        self.closed = False
        self.write("events", {"kind": "start", "started_at": start, "endpoint": ENDPOINT,
                   "topics": TOPICS, "duration_seconds": duration, "max_bytes": max_bytes,
                   "sample_interval_seconds": 1, "freshness_seconds_exclusive": 5,
                   "model_calls": 0, "real_orders": 0})

    def write(self, name: str, value: dict, *, terminal: bool = False) -> None:
        data = line(value)
        cap = self.limit if terminal else self.limit - TERMINAL_RESERVE
        if self.used + len(data) > cap:
            raise StopCapture("STORAGE_QUOTA")
        self.files[name].write(data)
        self.files[name].flush()
        self.used += len(data)

    def clock(self, at: datetime, mono: float) -> None:
        at = at.astimezone(timezone.utc) if at.tzinfo is not None and at.utcoffset() is not None else None
        if at is None or not math.isfinite(mono) or mono < self.last_mono or at < self.last_wall:
            raise StopCapture("LOCAL_CLOCK_REGRESSION")
        self.last_wall, self.last_mono = at, mono

    def reconnect(self, at: datetime, mono: float) -> None:
        self.clock(at, mono)
        self.quotes.clear()
        self.source_clocks.clear()
        self.generation += 1
        self.write("events", {"kind": "connection_reset", "at": stamp(at),
                              "elapsed_seconds": mono-self.start_mono, "generation": self.generation})

    def ingest(self, payload: bytes, at: datetime, mono: float) -> None:
        self.clock(at, mono)
        if mono - self.start_mono >= self.duration:
            raise StopCapture("DURATION")
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        self.write("raw", {"seq": self.seq + 1, "received_at": stamp(at),
                  "elapsed_seconds": mono-self.start_mono, "generation": self.generation,
                  "sha256": hashlib.sha256(payload).hexdigest(),
                  "payload_base64": base64.b64encode(payload).decode("ascii")})
        self.seq += 1
        symbol = None
        try:
            obj = json.loads(payload)
            if not isinstance(obj, dict):
                raise ValueError("invalid payload")
            if obj.get("op") == "subscribe" and obj.get("success") is False:
                self.quotes.clear()
                self.write("events", {"kind": "subscription_failure", "raw_seq": self.seq,
                                      "at": stamp(at), "generation": self.generation})
                raise StopCapture("SUBSCRIPTION_REJECTED")
            topic = obj.get("topic")
            if topic not in TOPICS:
                return
            symbol = topic.removeprefix("orderbook.1.")
            data = obj["data"]
            if obj["type"] != "snapshot" or data["s"] != symbol:
                raise ValueError("snapshot mismatch")
            if isinstance(obj["cts"], bool) or not isinstance(obj["cts"], int) or obj["cts"] < 0:
                raise ValueError("timestamps")
            # Bybit documents ts as a number but also publishes a string example.
            ts = obj["ts"]
            if isinstance(ts, bool) or not (isinstance(ts, int) and ts >= 0 or
                                            isinstance(ts, str) and ts.isascii() and ts.isdigit()):
                raise ValueError("timestamps")
            if any(isinstance(data[k], bool) or not isinstance(data[k], int) or data[k] < 0 for k in ("u", "seq")):
                raise ValueError("sequence")
            previous = self.source_clocks.get(symbol)
            restarting = False
            if previous is not None:
                if obj["cts"] < previous[1]:
                    raise ValueError("SOURCE_CLOCK_REGRESSION")
                if data["seq"] < previous[0]:
                    if data["u"] != 1:
                        raise ValueError("SOURCE_CLOCK_REGRESSION")
                    restarting = True
            b, a = data["b"], data["a"]
            if len(b) != 1 or len(a) != 1 or len(b[0]) != 2 or len(a[0]) != 2:
                raise ValueError("missing level")
            bid, bid_size, ask, ask_size = map(float, (b[0][0], b[0][1], a[0][0], a[0][1]))
            exchange = datetime.fromtimestamp(obj["cts"] / 1000, timezone.utc)
            row = dict(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, exchange_at=stamp(exchange))
            Frame.parse(dict(id="validate", received_at=stamp(at), quotes={symbol: row}))
            if restarting:
                self.quotes.pop(symbol, None)
                self.write("events", {"kind": "source_restart", "symbol": symbol,
                           "raw_seq": self.seq, "at": stamp(at), "generation": self.generation})
            # Identical update IDs are normal Bybit L1 keepalives; receipt is new.
            self.quotes[symbol] = (row, at.astimezone(timezone.utc), mono, self.seq)
            self.source_clocks[symbol] = (data["seq"], obj["cts"])
        except (KeyError, ValueError, TypeError, OverflowError, OSError, ReplayError) as exc:
            if symbol:
                self.quotes.pop(symbol, None)
                self.write("events", {"kind": "source_integrity_rejection", "symbol": symbol,
                           "raw_seq": self.seq, "at": stamp(at), "generation": self.generation,
                           "reason": "SOURCE_CLOCK_REGRESSION" if str(exc) == "SOURCE_CLOCK_REGRESSION" else "INVALID_SNAPSHOT"})
            else:
                self.quotes.clear()
                self.write("events", {"kind": "source_integrity_rejection", "symbol": None,
                           "raw_seq": self.seq, "at": stamp(at), "generation": self.generation,
                           "reason": "INVALID_PAYLOAD"})

    def sample(self, at: datetime, mono: float) -> None:
        self.clock(at, mono)
        if mono - self.start_mono >= self.duration:
            raise StopCapture("DURATION")
        if mono - self.last_frame_mono >= 300:
            raise StopCapture("NO_VALID_BOOK")
        if mono < self.next_tick:
            return
        # Never backdate a missed tick or use a later message for an earlier tick.
        skipped = max(0, int(mono - self.next_tick))
        self.next_tick = mono + 1
        reasons = []
        for symbol in SYMBOLS:
            q = self.quotes.get(symbol)
            if q is None or not 0 <= mono-q[2] < 5 or not 0 <= (at-q[1]).total_seconds() < 5:
                reasons.append(symbol)
            elif not 0 <= (at-datetime.fromisoformat(q[0]["exchange_at"])).total_seconds() < 5:
                reasons.append(symbol)
        if skipped:
            self.missed += skipped
            self.write("events", {"kind": "missed_ticks", "count": skipped, "at": stamp(at), "reason": "SAMPLER_DELAY"})
        if reasons:
            self.missed += 1
            self.write("events", {"kind": "missed_frame", "at": stamp(at), "symbols": reasons,
                                  "reason": "MISSING_STALE_OR_CLOCK_SKEW"})
            return
        if self.last_frame_at is not None and at <= self.last_frame_at:
            raise StopCapture("LOCAL_CLOCK_NOT_ADVANCING")
        packet = {"id": f"bybit-{self.frames+1:08d}", "received_at": stamp(at),
                  "quotes": {s: self.quotes[s][0] for s in SYMBOLS}}
        Frame.parse(packet)
        provenance = {"kind": "frame_provenance", "frame_id": packet["id"],
                      "generation": self.generation, "sample_elapsed_seconds": mono-self.start_mono,
                      "raw_cutoff_seq": self.seq,
                      "source_seqs": {s: self.quotes[s][3] for s in SYMBOLS}}
        if self.used + len(line(packet)) + len(line(provenance)) > self.limit - TERMINAL_RESERVE:
            raise StopCapture("STORAGE_QUOTA")
        self.write("frames", packet)
        self.frames += 1
        self.last_frame_at = at
        self.last_frame_mono = mono
        self.write("events", provenance)

    def close(self, reason: str, at: datetime, mono: float) -> None:
        if self.closed:
            return
        try:
            # Account for unsampled scheduled slots without inventing their wall times.
            unsampled = max(0, math.ceil(min(mono, self.start_mono+self.duration) - self.next_tick))
            self.write("events", {"kind": "terminal", "reason": reason, "at": stamp(at),
                       "elapsed_seconds": max(0, mono-self.start_mono), "raw_count": self.seq,
                       "frame_count": self.frames, "missed_count": self.missed,
                       "unsampled_due_ticks": unsampled,
                       "bytes_before_terminal": self.used}, terminal=True)
        finally:
            for stream in self.files.values():
                stream.close()
            self.closed = True


async def network(capture: Capture) -> str:
    try:
        import websockets
        from websockets.exceptions import WebSocketException
    except ImportError:
        return "WEBSOCKETS_UNAVAILABLE"
    now = lambda: datetime.now(timezone.utc)
    failures = 0
    while True:
        try:
            if time.monotonic() - capture.start_mono >= capture.duration:
                return "DURATION" if capture.frames else "NO_VALID_BOOK"
            if time.monotonic() - capture.last_frame_mono >= 300:
                return "NO_VALID_BOOK"
            capture.reconnect(now(), time.monotonic())
            remaining = min(capture.start_mono + capture.duration, capture.last_frame_mono + 300) - time.monotonic()
            if remaining <= 0:
                return "DURATION" if capture.frames else "NO_VALID_BOOK"
            async with websockets.connect(ENDPOINT, max_size=1_000_000, open_timeout=min(10, remaining)) as ws:
                remaining = min(capture.start_mono + capture.duration, capture.last_frame_mono + 300) - time.monotonic()
                if remaining <= 0:
                    return "DURATION" if capture.frames else "NO_VALID_BOOK"
                await asyncio.wait_for(ws.send(json.dumps({"op": "subscribe", "args": TOPICS})), remaining)
                next_ping = time.monotonic() + 20
                while True:
                    remaining = min(capture.start_mono + capture.duration, capture.last_frame_mono + 300) - time.monotonic()
                    if remaining <= 0:
                        return "NO_VALID_BOOK" if time.monotonic() - capture.last_frame_mono >= 300 else ("DURATION" if capture.frames else "NO_VALID_BOOK")
                    if time.monotonic() >= next_ping:
                        await asyncio.wait_for(ws.send('{"op":"ping"}'), remaining)
                        next_ping = time.monotonic() + 20
                    timeout = max(0.001, min(remaining, capture.next_tick-time.monotonic(), 1.0))
                    try:
                        # decode=False retains original WS text payload bytes.
                        payload = await asyncio.wait_for(ws.recv(decode=False), timeout)
                        at, mono = now(), time.monotonic()
                        capture.sample(at, mono)
                        capture.ingest(payload, at, mono)
                        failures = 0
                    except asyncio.TimeoutError:
                        capture.sample(now(), time.monotonic())
                    except TypeError:
                        return "WEBSOCKETS_INCOMPATIBLE"
        except StopCapture as exc:
            return "NO_VALID_BOOK" if str(exc) == "DURATION" and not capture.frames else str(exc)
        except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
            capture.reconnect(now(), time.monotonic())
            failures += 1
            response = getattr(exc, "response", None)
            http_status = getattr(response, "status_code", None)
            capture.write("events", {"kind": "connection_failure", "at": stamp(now()),
                                      "consecutive_failures": failures,
                                      "exception_type": type(exc).__name__,
                                      "http_status": http_status if type(http_status) is int else None})
            # An HTTP 200 is not a WebSocket 101 upgrade; retrying for hours
            # through the same non-upgrading proxy cannot produce book data.
            if http_status == 200:
                return "WS_UPGRADE_NOT_AVAILABLE"
            if failures >= 20:
                return "CONNECTION_RETRY_LIMIT"
            await asyncio.sleep(min(retry_delay(failures), max(0, min(capture.start_mono+capture.duration,
                                capture.last_frame_mono+300)-time.monotonic())))


def retry_delay(failures: int) -> int:
    return min(30, 2 ** min(failures - 1, 5))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--enable-public-network", action="store_true")
    args = parser.parse_args(argv)
    if not args.enable_public_network:
        parser.error("explicit --enable-public-network is required")
    try:
        capture = Capture(Path(args.output_dir), datetime.now(timezone.utc), time.monotonic(),
                          args.duration_seconds, args.max_bytes)
        reason = "INTERRUPTED"
        try:
            try:
                reason = asyncio.run(network(capture))
            except StopCapture as exc:
                reason = str(exc)
            except OSError:
                reason = "IO_ERROR"
        finally:
            capture.close(reason, datetime.now(timezone.utc), time.monotonic())
        print(json.dumps({"status": reason, "frames": capture.frames, "missed": capture.missed}))
        return 0 if reason == "DURATION" else 2
    except (OSError, ValueError, StopCapture, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
