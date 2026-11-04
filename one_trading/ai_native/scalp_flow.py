"""ONE Flow Scalper: public linear-perpetual tape -> bounded PAPER responses.

No authentication, private endpoints, AI/model calls or real order routes exist.
Every apparent fill is a deliberately conservative *simulation* on a later book.
Run ``python -m one_trading.ai_native.scalp_flow --help`` for entry points.
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
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
VERSION = "scalp-flow-paper-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]


class ScalpError(ValueError):
    pass


def encode(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def milliseconds(value: str) -> int:
    if not isinstance(value, str):
        raise ScalpError("TIMESTAMP_REQUIRED")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScalpError("INVALID_TIMESTAMP") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ScalpError("TIMEZONE_REQUIRED")
    return int(dt.timestamp() * 1000)


def timestamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def finite(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScalpError("BAD_NUMERIC_VALUE")
    try:
        n = float(value)
    except ValueError as exc:
        raise ScalpError("BAD_NUMERIC_VALUE") from exc
    if not math.isfinite(n) or (positive and n <= 0):
        raise ScalpError("BAD_NUMERIC_VALUE")
    return n


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ScalpError("INVALID_INTEGER")
    return value


def rounded(value: float) -> float:
    return round(value, 8)


@dataclass(frozen=True)
class Settings:
    symbol: str = "SOLUSDT"
    capital_usdt: float = 10_000.0
    leverage: int = 3
    max_margin_fraction: float = 0.10
    risk_fraction: float = 0.005
    daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.08
    fee_bps: float = 5.5
    slippage_bps: float = 3.0
    stop_bps: float = 35.0
    target_bps: float = 80.0
    max_spread_bps: float = 5.0
    max_book_participation: float = 0.03
    min_notional_usdt: float = 50.0
    min_tape_usdt: float = 100_000.0
    min_trade_count: int = 15
    tape_imbalance_min: float = 0.25
    book_imbalance_min: float = 0.12
    min_price_response_bps: float = 0.6
    flow_window_ms: int = 15_000
    book_fresh_ms: int = 1_000
    execution_delay_ms: int = 250
    entry_expiry_ms: int = 2_000
    max_hold_ms: int = 90_000
    cooldown_ms: int = 15_000
    max_loss_streak: int = 4

    def __post_init__(self) -> None:
        if self.symbol not in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            raise ScalpError("UNSUPPORTED_LINEAR_SYMBOL")
        for key, value in vars(self).items():
            if key != "symbol" and not math.isfinite(float(value)):
                raise ScalpError("NONFINITE_SETTING:" + key)
        if not 1 <= self.leverage <= 3:
            raise ScalpError("LEVERAGE_CAP_3X")
        for key in ("max_margin_fraction", "risk_fraction", "daily_loss_fraction",
                    "max_drawdown_fraction", "max_book_participation",
                    "tape_imbalance_min", "book_imbalance_min"):
            if not 0 < getattr(self, key) < 1:
                raise ScalpError("INVALID_FRACTION:" + key)
        for key in ("capital_usdt", "fee_bps", "slippage_bps", "stop_bps",
                    "target_bps", "max_spread_bps", "min_notional_usdt",
                    "min_tape_usdt", "min_trade_count", "min_price_response_bps",
                    "flow_window_ms", "book_fresh_ms", "execution_delay_ms",
                    "entry_expiry_ms", "max_hold_ms", "cooldown_ms", "max_loss_streak"):
            if getattr(self, key) <= 0:
                raise ScalpError("NONPOSITIVE_SETTING:" + key)
        if self.risk_fraction > 0.01 or self.max_margin_fraction > 0.20 or self.daily_loss_fraction > 0.05:
            raise ScalpError("PAPER_RISK_CAP_EXCEEDED")
        if self.target_bps <= 2 * (self.fee_bps + self.slippage_bps):
            raise ScalpError("TARGET_DOES_NOT_COVER_ROUND_TRIP_COST")
        if self.execution_delay_ms >= self.entry_expiry_ms:
            raise ScalpError("ENTRY_EXPIRES_BEFORE_ELIGIBILITY")


@dataclass
class Book:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    received_ms: int
    source_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000

    @property
    def imbalance(self) -> float:
        return (self.bid_size - self.ask_size) / (self.bid_size + self.ask_size)


@dataclass
class Position:
    side: int
    quantity: float
    entry_price: float
    entered_ms: int
    entry_fee: float
    entry_notional: float
    closed_quantity: float = 0.0
    closed_pnl: float = 0.0
    exit_fees: float = 0.0


@dataclass
class Pending:
    purpose: str
    side: int
    decided_ms: int
    eligible_ms: int
    expires_ms: int
    reference_mid: float
    reason: str


class ScalpEngine:
    """Deterministic, receipt-time-only single-symbol state machine."""

    def __init__(self, config: Settings):
        self.c = config
        self.book: Book | None = None
        self.trades: deque[tuple[int, float]] = deque()
        self.mids: deque[tuple[int, float]] = deque()
        self.seen_trade_ids: set[str] = set()
        self.trade_id_receipts: deque[tuple[int, str]] = deque()
        self.source_seq: int | None = None
        self.source_ms: int | None = None
        self.position: Position | None = None
        self.pending: Pending | None = None
        self.cash = config.capital_usdt
        self.peak = config.capital_usdt
        self.daily_start = config.capital_usdt
        self.day: str | None = None
        self.halted = False
        self.loss_streak = 0
        self.cooldown_until = 0
        self.next_evaluation_ms = 0
        self.last_received_ms = -1
        self.signals = self.entry_fills = self.exits = self.rejected = 0
        self.trades_closed = 0

    def reset_feed(self) -> None:
        # A reconnect invalidates cached tape/book; pending exits remain necessary.
        self.book = None
        self.trades.clear()
        self.mids.clear()
        self.seen_trade_ids.clear()
        self.trade_id_receipts.clear()
        self.source_seq = self.source_ms = None
        if self.pending is not None and self.pending.purpose == "ENTRY":
            self.pending = None

    def _fresh(self, at: int) -> bool:
        return self.book is not None and 0 <= at - self.book.received_ms <= self.c.book_fresh_ms

    def _exit_price(self, side: int) -> float:
        assert self.book is not None
        px = self.book.bid if side == 1 else self.book.ask
        return px * (1 - side * self.c.slippage_bps / 10_000)

    def equity(self) -> float:
        if self.position is None or self.book is None:
            return self.cash
        p = self.position
        mark = self._exit_price(p.side)
        floating = p.side * (mark - p.entry_price) * p.quantity
        future_fee = mark * p.quantity * self.c.fee_bps / 10_000
        return self.cash + floating - future_fee

    def _prune(self, at: int) -> None:
        window = self.c.flow_window_ms
        while self.trades and at - self.trades[0][0] > window:
            self.trades.popleft()
        while self.mids and at - self.mids[0][0] > window:
            self.mids.popleft()
        while self.trade_id_receipts and at - self.trade_id_receipts[0][0] > window:
            _, tid = self.trade_id_receipts.popleft()
            self.seen_trade_ids.discard(tid)

    def _risk(self, at: int, out: list[dict[str, Any]]) -> None:
        day = timestamp(at)[:10]
        if self.day != day:
            self.day = day
            self.daily_start = self.equity()
            self.loss_streak = 0
            self.halted = False if self.equity() > self.c.capital_usdt * 0.5 else self.halted
        equity = self.equity()
        self.peak = max(self.peak, equity)
        reason = None
        if equity <= self.daily_start * (1 - self.c.daily_loss_fraction):
            reason = "DAILY_LOSS"
        if equity <= self.peak * (1 - self.c.max_drawdown_fraction):
            reason = "MAX_DRAWDOWN"
        if self.loss_streak >= self.c.max_loss_streak:
            reason = "LOSS_STREAK"
        if reason and not self.halted:
            self.halted = True
            out.append({"kind": "risk_halt", "reason": reason, "at": timestamp(at),
                        "equity_usdt": rounded(equity)})

    def _queue(self, purpose: str, side: int, at: int, reason: str,
               out: list[dict[str, Any]]) -> None:
        assert self.book is not None
        self.pending = Pending(purpose, side, at, at + self.c.execution_delay_ms,
                               at + (self.c.entry_expiry_ms if purpose == "ENTRY" else self.c.max_hold_ms),
                               self.book.mid, reason)
        out.append({"kind": "order_queued", "purpose": purpose, "side": side,
                    "decided_at": timestamp(at), "eligible_at": timestamp(self.pending.eligible_ms),
                    "reference_mid": rounded(self.book.mid), "reason": reason})

    def _fill(self, at: int, out: list[dict[str, Any]]) -> None:
        p = self.pending
        if p is None or at < p.eligible_ms or not self._fresh(at):
            return
        assert self.book is not None
        if at <= p.decided_ms:
            return  # absolutely no same-receipt fill
        book = self.book
        if p.purpose == "ENTRY":
            self.pending = None
            if at > p.expires_ms or self.halted:
                out.append({"kind": "no_fill", "reason": "EXPIRED_OR_HALTED", "at": timestamp(at)})
                return
            drift = abs(book.mid / p.reference_mid - 1) * 10_000
            if book.spread_bps > self.c.max_spread_bps or drift > self.c.stop_bps / 2:
                out.append({"kind": "no_fill", "reason": "SPREAD_OR_PRICE_GAP", "at": timestamp(at)})
                return
            px = (book.ask if p.side == 1 else book.bid) * (1 + p.side * self.c.slippage_bps / 10_000)
            exit_depth = (book.ask_size if p.side == 1 else book.bid_size) * px
            one_round_trip_cost = (self.c.stop_bps + 2 * (self.c.fee_bps + self.c.slippage_bps)) / 10_000
            notional = min(self.equity() * self.c.max_margin_fraction * self.c.leverage,
                           self.equity() * self.c.risk_fraction / one_round_trip_cost,
                           exit_depth * self.c.max_book_participation)
            if notional < self.c.min_notional_usdt:
                out.append({"kind": "no_fill", "reason": "INSUFFICIENT_BOOK_CAPACITY", "at": timestamp(at),
                            "maximum_notional_usdt": rounded(notional)})
                return
            qty = notional / px
            fee = notional * self.c.fee_bps / 10_000
            self.cash -= fee
            self.position = Position(p.side, qty, px, at, fee, notional)
            self.entry_fills += 1
            out.append({"kind": "paper_fill", "purpose": "ENTRY", "side": p.side,
                        "at": timestamp(at), "price": rounded(px), "quantity": rounded(qty),
                        "notional_usdt": rounded(notional), "fee_usdt": rounded(fee),
                        "equity_usdt": rounded(self.equity()), "basis": "L1_DELAYED_TAKER_SIMULATION"})
            return
        pos = self.position
        if pos is None:
            self.pending = None
            return
        px = self._exit_price(pos.side)
        available_qty = (book.bid_size if pos.side == 1 else book.ask_size) * self.c.max_book_participation
        qty = min(pos.quantity, available_qty)
        if qty * px < self.c.min_notional_usdt and qty < pos.quantity * (1 - 1e-9):
            out.append({"kind": "no_fill", "reason": "EXIT_DEPTH_TOO_THIN", "at": timestamp(at)})
            return
        if qty <= 0:
            return
        fee = qty * px * self.c.fee_bps / 10_000
        pnl = pos.side * (px - pos.entry_price) * qty - fee
        self.cash += pnl
        pos.quantity -= qty
        pos.closed_quantity += qty
        pos.closed_pnl += pnl
        pos.exit_fees += fee
        self.exits += 1
        out.append({"kind": "paper_fill", "purpose": "EXIT", "side": -pos.side,
                    "at": timestamp(at), "price": rounded(px), "quantity": rounded(qty),
                    "fee_usdt": rounded(fee), "realized_usdt_excluding_entry_fee": rounded(pnl),
                    "remaining_quantity": rounded(max(0, pos.quantity)),
                    "reason": p.reason, "basis": "L1_DELAYED_TAKER_SIMULATION"})
        if pos.quantity <= 1e-10:
            net = pos.closed_pnl - pos.entry_fee
            self.trades_closed += 1
            self.loss_streak = self.loss_streak + 1 if net < 0 else 0
            self.cooldown_until = at + self.c.cooldown_ms
            self.position = self.pending = None
            out.append({"kind": "trade_closed", "at": timestamp(at),
                        "net_pnl_usdt": rounded(net), "equity_usdt": rounded(self.cash),
                        "loss_streak": self.loss_streak})

    def _observe(self, at: int, out: list[dict[str, Any]]) -> None:
        if at < self.next_evaluation_ms or self.position or self.pending or self.halted or at < self.cooldown_until:
            return
        self.next_evaluation_ms = at + 1_000
        assert self.book is not None
        total = sum(abs(notional) for _, notional in self.trades)
        signed = sum(notional for _, notional in self.trades)
        tape = signed / total if total else 0.0
        book = self.book.imbalance
        response = (self.book.mid / self.mids[0][1] - 1) * 10_000 if self.mids else 0.0
        span = at - self.mids[0][0] if self.mids else 0
        reason = "NO_TRADE_PRESSURE"
        side = 0
        if (len(self.trades) >= self.c.min_trade_count and total >= self.c.min_tape_usdt
                and span >= 3_000 and self.book.spread_bps <= self.c.max_spread_bps):
            if tape >= self.c.tape_imbalance_min and book >= self.c.book_imbalance_min and response >= self.c.min_price_response_bps:
                side = 1
            elif tape <= -self.c.tape_imbalance_min and book <= -self.c.book_imbalance_min and response <= -self.c.min_price_response_bps:
                side = -1
            else:
                reason = "FLOW_AND_PRICE_NOT_ALIGNED"
        out.append({"kind": "observation", "at": timestamp(at), "side": side,
                    "tape_imbalance": rounded(tape), "book_imbalance": rounded(book),
                    "price_response_bps": rounded(response), "tape_usdt": rounded(total),
                    "trade_count": len(self.trades), "reason": "ALIGNED_FLOW" if side else reason})
        if side:
            self.signals += 1
            self._queue("ENTRY", side, at, "ALIGNED_FLOW", out)

    def ingest(self, received_ms: int, raw: bytes) -> list[dict[str, Any]]:
        if received_ms < self.last_received_ms:
            raise ScalpError("RECEIPT_CLOCK_REGRESSION")
        self.last_received_ms = received_ms
        out: list[dict[str, Any]] = []
        self._prune(received_ms)
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ScalpError("PAYLOAD_NOT_OBJECT")
            if msg.get("op") == "subscribe" and msg.get("success") is False:
                raise ScalpError("SUBSCRIPTION_REJECTED")
            topic = msg.get("topic")
            if topic == "orderbook.1." + self.c.symbol:
                data = msg["data"]
                if not isinstance(data, dict) or msg.get("type") != "snapshot" or data.get("s") != self.c.symbol:
                    raise ScalpError("BOOK_NOT_SNAPSHOT")
                cts, seq = integer(msg["cts"]), integer(data["seq"])
                u = integer(data["u"])
                if cts > received_ms + 2_000 or received_ms - cts > 2_000:
                    raise ScalpError("BOOK_SOURCE_CLOCK_SKEW")
                if self.source_ms is not None and cts < self.source_ms:
                    raise ScalpError("BOOK_CLOCK_REGRESSION")
                if self.source_seq is not None and seq < self.source_seq and u != 1:
                    raise ScalpError("BOOK_SEQUENCE_REGRESSION")
                b, a = data["b"], data["a"]
                if len(b) != 1 or len(a) != 1 or len(b[0]) != 2 or len(a[0]) != 2:
                    raise ScalpError("BOOK_LEVEL_MISSING")
                bid, bs = (finite(v, positive=True) for v in b[0])
                ask, az = (finite(v, positive=True) for v in a[0])
                if bid >= ask:
                    raise ScalpError("BOOK_CROSSED")
                self.book = Book(bid, ask, bs, az, received_ms, cts)
                self.source_ms, self.source_seq = cts, seq
                self.mids.append((received_ms, self.book.mid))
                self._risk(received_ms, out)
                if self.position and self.pending is None:
                    pos = self.position
                    signed_change_bps = pos.side * (self.book.mid / pos.entry_price - 1) * 10_000
                    age = received_ms - pos.entered_ms
                    if self.halted or signed_change_bps <= -self.c.stop_bps or signed_change_bps >= self.c.target_bps or age >= self.c.max_hold_ms:
                        reason = ("RISK_HALT" if self.halted else "STOP" if signed_change_bps <= -self.c.stop_bps
                                  else "TARGET" if signed_change_bps >= self.c.target_bps else "TIME")
                        self._queue("EXIT", pos.side, received_ms, reason, out)
                elif self.pending:
                    self._fill(received_ms, out)
                if self._fresh(received_ms):
                    self._observe(received_ms, out)
            elif topic == "publicTrade." + self.c.symbol:
                rows = msg["data"]
                if not isinstance(rows, list) or len(rows) > 2_000:
                    raise ScalpError("TRADE_BATCH_INVALID")
                accepted: list[tuple[str, float]] = []
                for row in rows:
                    if not isinstance(row, dict) or row.get("s") != self.c.symbol or row.get("S") not in ("Buy", "Sell"):
                        raise ScalpError("TRADE_SCHEMA_INVALID")
                    exchange_ms = integer(row["T"])
                    if exchange_ms > received_ms + 2_000 or received_ms - exchange_ms > 5_000:
                        raise ScalpError("TRADE_CLOCK_SKEW")
                    trade_id = row["i"]
                    if not isinstance(trade_id, str) or not 1 <= len(trade_id) <= 160:
                        raise ScalpError("TRADE_ID_INVALID")
                    value = finite(row["p"], positive=True) * finite(row["v"], positive=True)
                    accepted.append((trade_id, value if row["S"] == "Buy" else -value))
                for trade_id, value in accepted:
                    if trade_id not in self.seen_trade_ids:
                        self.seen_trade_ids.add(trade_id)
                        self.trade_id_receipts.append((received_ms, trade_id))
                        self.trades.append((received_ms, value))
            # Other public topics, heartbeat replies and subscription ACKs are inert.
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            self.rejected += 1
            if isinstance(msg if "msg" in locals() else None, dict) and str(msg.get("topic", "")).startswith("orderbook.1."):
                self.book = None
                self.mids.clear()
            out.append({"kind": "input_rejected", "at": timestamp(received_ms), "reason": str(exc)[:160]})
        return out

    def report(self) -> dict[str, Any]:
        return {"equity_usdt": rounded(self.equity()), "cash_usdt": rounded(self.cash),
                "net_pnl_usdt": rounded(self.equity() - self.c.capital_usdt),
                "signals": self.signals, "entry_fills": self.entry_fills,
                "exit_fills": self.exits, "trades_closed": self.trades_closed,
                "rejected_messages": self.rejected, "halted": self.halted,
                "open_quantity": rounded(self.position.quantity) if self.position else 0.0,
                "pending_order": self.pending.purpose if self.pending else None,
                "execution": "PAPER_ONLY_NOT_EXECUTABLE_PNL"}


class Journal:
    def __init__(self, output_dir: Path, config: Settings, mode: str):
        output_dir = output_dir.expanduser().resolve()
        if output_dir == REPO_ROOT or REPO_ROOT in output_dir.parents:
            raise ScalpError("PRIVATE_LEDGER_REQUIRED_OUTSIDE_REPOSITORY")
        output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.path = output_dir / "events.jsonl"
        self.file = self.path.open("xb")
        os.chmod(self.path, 0o600)
        self.tip = "0" * 64
        self.seq = 0
        self.bytes_written = 0
        self.cap_bytes = 200_000_000
        self.append("start", {"version": VERSION, "config": asdict(config), "mode": mode,
                              "endpoint": ENDPOINT, "real_orders": 0, "model_calls": 0})

    def append(self, kind: str, payload: dict[str, Any], *, terminal: bool = False) -> None:
        row = {"seq": self.seq, "previous_hash": self.tip, "kind": kind, "payload": payload}
        row["hash"] = digest(encode(row))
        data = encode(row) + b"\n"
        if self.bytes_written + len(data) > self.cap_bytes - (0 if terminal else 4096):
            raise ScalpError("JOURNAL_SIZE_BOUND")
        self.file.write(data)
        self.file.flush()
        self.bytes_written += len(data)
        self.tip = row["hash"]
        self.seq += 1

    def close(self) -> None:
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()


class Session:
    def __init__(self, output_dir: Path, config: Settings, mode: str):
        self.engine = ScalpEngine(config)
        self.journal = Journal(output_dir, config, mode)
        self.inputs = 0

    def ingest(self, at: int, raw: bytes) -> None:
        self.journal.append("input", {"received_at": timestamp(at),
                        "payload_base64": base64.b64encode(raw).decode("ascii"),
                        "sha256": digest(raw)})
        self.inputs += 1
        for row in self.engine.ingest(at, raw):
            self.journal.append(row.pop("kind"), row)

    def reset_feed(self, at: int, reason: str) -> None:
        self.engine.reset_feed()
        self.journal.append("feed_reset", {"at": timestamp(at), "reason": reason})

    def close(self, reason: str) -> dict[str, Any]:
        summary = {"reason": reason, "inputs": self.inputs, **self.engine.report()}
        try:
            self.journal.append("terminal", summary, terminal=True)
        finally:
            self.journal.close()
        return summary


def verify_journal(path: Path) -> dict[str, Any]:
    tip, count, engine, pending, inputs, ended = "0" * 64, 0, None, [], 0, False
    with path.open("rb") as file:
        for line in file:
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {"seq", "previous_hash", "kind", "payload", "hash"}:
                    raise ScalpError("JOURNAL_ROW_SCHEMA")
                signed = {k: row[k] for k in ("seq", "previous_hash", "kind", "payload")}
                if row["seq"] != count or row["previous_hash"] != tip or digest(encode(signed)) != row["hash"]:
                    raise ScalpError("JOURNAL_HASH_CHAIN_INVALID")
                tip, count = row["hash"], count + 1
                kind, payload = row["kind"], row["payload"]
                if ended:
                    raise ScalpError("EVENTS_AFTER_TERMINAL")
                if count == 1:
                    if kind != "start" or payload["version"] != VERSION or payload["real_orders"] or payload["model_calls"]:
                        raise ScalpError("INVALID_START")
                    engine = ScalpEngine(Settings(**payload["config"]))
                elif kind == "input":
                    if pending:
                        raise ScalpError("MISSING_DERIVED_EVENT")
                    raw = base64.b64decode(payload["payload_base64"], validate=True)
                    if digest(raw) != payload["sha256"]:
                        raise ScalpError("INPUT_SHA_MISMATCH")
                    pending.extend(engine.ingest(milliseconds(payload["received_at"]), raw))
                    inputs += 1
                elif kind == "feed_reset":
                    if pending:
                        raise ScalpError("MISSING_DERIVED_EVENT")
                    engine.reset_feed()
                elif kind == "terminal":
                    if pending:
                        raise ScalpError("MISSING_DERIVED_EVENT")
                    if payload != {"reason": payload["reason"], "inputs": inputs, **engine.report()}:
                        raise ScalpError("TERMINAL_REPORT_MISMATCH")
                    ended = True
                else:
                    if not pending:
                        raise ScalpError("UNEXPECTED_DERIVED_EVENT")
                    expected = pending.pop(0)
                    if kind != expected.pop("kind") or payload != expected:
                        raise ScalpError("DERIVED_EVENT_MISMATCH")
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                if isinstance(exc, ScalpError):
                    raise
                raise ScalpError("MALFORMED_JOURNAL") from exc
    if not ended:
        raise ScalpError("MISSING_TERMINAL")
    return {"status": "PASS", "events": count, "inputs": inputs,
            "source_and_decision_replay": "IDENTICAL", **engine.report()}


def replay(input_file: Path, session: Session) -> dict[str, Any]:
    reason = "REPLAY_COMPLETE"
    try:
        with input_file.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {"received_at", "payload"} or not isinstance(row["payload"], dict):
                    raise ScalpError("REPLAY_INPUT_SCHEMA_LINE:" + str(line_no))
                session.ingest(milliseconds(row["received_at"]), encode(row["payload"]))
    except Exception:
        reason = "REPLAY_ABORTED"
        raise
    finally:
        summary = session.close(reason)
    return summary


async def public_network(session: Session, duration: int, max_inputs: int) -> dict[str, Any]:
    reason = "FAILED"
    try:
        try:
            import websockets
        except ImportError as exc:
            raise ScalpError("INSTALL_WEBSOCKETS_FOR_PUBLIC_NETWORK") from exc
        start = time.monotonic()
        attempts = 0
        reason = "DURATION"
        while time.monotonic() - start < duration and session.inputs < max_inputs:
            at = int(time.time() * 1000)
            session.reset_feed(at, "CONNECT_OR_RECONNECT")
            try:
                async with websockets.connect(ENDPOINT, open_timeout=10, max_size=2_000_000,
                                              ping_interval=20) as socket:
                    await socket.send(json.dumps({"op": "subscribe", "args": [
                        "orderbook.1." + session.engine.c.symbol,
                        "publicTrade." + session.engine.c.symbol]}))
                    attempts = 0
                    while time.monotonic() - start < duration and session.inputs < max_inputs:
                        remaining = duration - (time.monotonic() - start)
                        try:
                            raw = await asyncio.wait_for(socket.recv(decode=False), timeout=min(5, remaining))
                        except asyncio.TimeoutError:
                            continue
                        if not isinstance(raw, bytes):
                            raise ScalpError("WEBSOCKET_BYTES_REQUIRED")
                        session.ingest(int(time.time() * 1000), raw)
                        try:
                            reply = json.loads(raw)
                        except ValueError:
                            reply = None
                        if isinstance(reply, dict) and reply.get("op") == "subscribe" and reply.get("success") is False:
                            raise ScalpError("SUBSCRIPTION_REJECTED")
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
                attempts += 1
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) == 200:
                    reason = "WEBSOCKET_UPGRADE_UNAVAILABLE"
                    break
                if attempts >= 5:
                    reason = "RECONNECT_LIMIT"
                    break
                await asyncio.sleep(min(2 ** attempts, max(0, duration - (time.monotonic() - start))))
        if session.inputs >= max_inputs:
            reason = "MAX_INPUTS"
    finally:
        # A failed connection or exceeded disk bound must still leave a terminal
        # record that an independent verifier can reproduce and classify.
        summary = session.close(reason)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("replay", help="Replay recorded public payloads; no network")
    offline.add_argument("--input", required=True)
    offline.add_argument("--output-dir", required=True)
    online = sub.add_parser("live", help="Public Bybit linear feed; PAPER fills only")
    online.add_argument("--enable-public-network", action="store_true")
    online.add_argument("--output-dir", required=True)
    online.add_argument("--duration-seconds", type=int, default=300)
    online.add_argument("--max-inputs", type=int, default=100_000)
    audit = sub.add_parser("verify", help="Independent hash-chain and decision replay")
    audit.add_argument("--events", required=True)
    for command in (offline, online):
        command.add_argument("--symbol", default="SOLUSDT", choices=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        command.add_argument("--capital-usdt", type=float, default=10_000.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_journal(Path(args.events))
        else:
            c = Settings(symbol=args.symbol, capital_usdt=args.capital_usdt)
            if args.command == "live":
                if not args.enable_public_network:
                    raise ScalpError("EXPLICIT_PUBLIC_NETWORK_OPT_IN_REQUIRED")
                if not 1 <= args.duration_seconds <= 3_600 or not 100 <= args.max_inputs <= 200_000:
                    raise ScalpError("LIVE_BOUNDS")
            session = Session(Path(args.output_dir), c, "PUBLIC_LINEAR_LIVE" if args.command == "live" else "OFFLINE_REPLAY")
            result = (replay(Path(args.input), session) if args.command == "replay"
                      else asyncio.run(public_network(session, args.duration_seconds, args.max_inputs)))
            result["journal"] = str(session.journal.path)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("status") == "PASS" or result.get("reason") in ("REPLAY_COMPLETE", "DURATION", "MAX_INPUTS") else 2
    except (ScalpError, OSError, ValueError, KeyboardInterrupt, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
