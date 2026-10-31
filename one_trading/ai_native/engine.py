"""Chronological, costed, multi-arm PAPER experiment for autonomous decisions.

All decisions see only the current received packet and *previously matured*
labels. Execution needs a later received quote, never the signal quote. AI
strategies are open-ended at the decision layer; portfolio risk is separate.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol as TypeProtocol


class ReplayError(ValueError):
    """Invalid or chronologically unusable research input."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayError("TIME_NOT_STRING")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError("BAD_TIME") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ReplayError("NAIVE_TIME")
    return stamp.astimezone(timezone.utc)


def numeric(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayError("NONNUMERIC_FIELD")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ReplayError("INVALID_NUMBER")
    return result


@dataclass(frozen=True)
class Protocol:
    capital_usdt: float = 10_000.0
    execution_delay_seconds: float = 2.0
    max_quote_age_seconds: float = 30.0
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 5.0
    max_spread_bps: float = 20.0
    max_book_participation: float = 0.01
    max_position_fraction: float = 0.125
    max_total_exposure_fraction: float = 0.4
    max_drawdown_fraction: float = 0.15
    min_notional_usdt: float = 10.0
    label_horizon_seconds: float = 3_600.0

    def __post_init__(self) -> None:
        for key, value in vars(self).items():
            numeric(value, positive=key != "execution_delay_seconds")
        if self.execution_delay_seconds < 0:
            raise ReplayError("NEGATIVE_EXECUTION_DELAY")
        if not 0 < self.max_book_participation <= 1:
            raise ReplayError("PARTICIPATION_LIMIT")
        for key in ("max_position_fraction", "max_total_exposure_fraction", "max_drawdown_fraction"):
            if not 0 < getattr(self, key) < 1:
                raise ReplayError("RISK_LIMIT")
        if self.max_position_fraction > self.max_total_exposure_fraction:
            raise ReplayError("POSITION_EXCEEDS_PORTFOLIO_CAP")


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    exchange_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000


@dataclass(frozen=True)
class Frame:
    id: str
    received_at: datetime
    quotes: dict[str, Quote]
    raw_hash: str

    @staticmethod
    def parse(raw: dict[str, Any]) -> "Frame":
        if not isinstance(raw, dict) or set(raw) != {"id", "received_at", "quotes"}:
            raise ReplayError("FRAME_SCHEMA")
        if not isinstance(raw["id"], str) or not 1 <= len(raw["id"]) <= 120:
            raise ReplayError("FRAME_ID")
        received = instant(raw["received_at"])
        rows = raw["quotes"]
        if not isinstance(rows, dict) or not rows or len(rows) > 100:
            raise ReplayError("FRAME_UNIVERSE")
        quotes: dict[str, Quote] = {}
        for symbol, row in rows.items():
            if not isinstance(symbol, str) or not symbol.isalnum() or len(symbol) > 24:
                raise ReplayError("BAD_SYMBOL")
            if not isinstance(row, dict) or set(row) != {"bid", "ask", "bid_size", "ask_size", "exchange_at"}:
                raise ReplayError("QUOTE_SCHEMA")
            quote = Quote(*(numeric(row[k], positive=True) for k in ("bid", "ask", "bid_size", "ask_size")),
                          instant(row["exchange_at"]))
            if quote.bid >= quote.ask or quote.exchange_at > received:
                raise ReplayError("CROSSED_OR_FUTURE_QUOTE")
            quotes[symbol] = quote
        return Frame(raw["id"], received, quotes, sha256(raw))


@dataclass(frozen=True)
class Plan:
    action: str
    symbol: str
    fraction: float
    p_up: float
    thesis: str
    counter_hypothesis: str
    invalidation: str

    @staticmethod
    def parse(raw: dict[str, Any], universe: set[str]) -> "Plan":
        fields = {"action", "symbol", "fraction", "p_up", "thesis", "counter_hypothesis", "invalidation"}
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ReplayError("PLAN_SCHEMA")
        action, symbol = raw["action"], raw["symbol"]
        if action not in ("BUY", "SELL", "HOLD") or symbol not in universe | {"CASH"}:
            raise ReplayError("PLAN_ACTION_OR_SYMBOL")
        fraction, p_up = numeric(raw["fraction"]), numeric(raw["p_up"])
        if not 0 <= fraction <= 1 or not 0 <= p_up <= 1:
            raise ReplayError("PLAN_PROBABILITY_OR_SIZE")
        if (action == "HOLD") != (fraction == 0) or (action != "HOLD" and symbol == "CASH"):
            raise ReplayError("PLAN_SIZE_INCONSISTENT")
        for key in ("thesis", "counter_hypothesis", "invalidation"):
            if not isinstance(raw[key], str) or not 1 <= len(raw[key]) <= 2_000:
                raise ReplayError("PLAN_REASONING_MISSING")
        return Plan(action, symbol, fraction, p_up, raw["thesis"],
                    raw["counter_hypothesis"], raw["invalidation"])


class Planner(TypeProtocol):
    def propose(self, context: dict[str, Any]) -> dict[str, Any] | None: ...


class Chain:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.tip = "0" * 64

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        rec = {"seq": len(self.records), "previous_hash": self.tip, "kind": kind, "payload": payload}
        rec["hash"] = sha256(rec)
        self.tip = rec["hash"]
        self.records.append(rec)


@dataclass
class Position:
    quantity: float
    cost_basis: float


@dataclass
class Arm:
    name: str
    planner: Planner
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    episodes: list[dict[str, Any]] = field(default_factory=list)
    matured: list[dict[str, Any]] = field(default_factory=list)
    last_episode_due: datetime | None = None
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    halted: bool = False
    model_unavailable: int = 0
    rejected_orders: int = 0
    fills: int = 0
    decisions: int = 0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    gross_turnover: float = 0.0
    worst_drawdown: float = 0.0


class Experiment:
    """One finite replay. Does not collect data or touch an existing account."""

    def __init__(self, protocol: Protocol, planners: dict[str, Planner], *, data_use: str):
        if data_use not in ("SYNTHETIC", "REUSED_HISTORICAL", "PROSPECTIVE_REPLAY"):
            raise ReplayError("DATA_USE_DECLARATION_REQUIRED")
        if not planners or len(planners) != len(set(planners)) or "CASH" not in planners:
            raise ReplayError("CASH_CONTROL_REQUIRED")
        self.protocol, self.data_use = protocol, data_use
        self.arms = {name: Arm(name, planner, protocol.capital_usdt, peak_equity=protocol.capital_usdt)
                     for name, planner in planners.items()}
        self.ledger = Chain()
        self._last: Frame | None = None
        self._seen: set[str] = set()
        self._universe: set[str] | None = None
        self._frames = 0
        self._raw_frames: list[dict[str, Any]] = []

    def _equity(self, arm: Arm, frame: Frame) -> tuple[float, float]:
        # Mark open positions at bid, which is closer to an executable exit.
        mark = sum(pos.quantity * frame.quotes[s].bid for s, pos in arm.positions.items())
        return arm.cash + mark, mark

    def _mature(self, arm: Arm, frame: Frame) -> None:
        for episode in arm.episodes:
            if episode["status"] != "PENDING" or frame.received_at < instant(episode["due_at"]):
                continue
            symbol = episode["symbol"]
            if symbol == "CASH":
                excess = 0.0
            else:
                age = (frame.received_at - frame.quotes[symbol].exchange_at).total_seconds()
                if not 0 <= age <= self.protocol.max_quote_age_seconds:
                    # The episode remains pending until a newly *received* fresh
                    # quote exists. A stale price is not a mature outcome.
                    continue
                exit_mid = frame.quotes[symbol].mid
                raw_return = exit_mid / episode["origin_mid"] - 1
                if episode["action"] == "BUY":
                    excess = raw_return - 2 * (self.protocol.fee_bps_per_side + self.protocol.slippage_bps_per_side) / 10_000
                elif episode["action"] == "SELL":
                    excess = -raw_return - 2 * (self.protocol.fee_bps_per_side + self.protocol.slippage_bps_per_side) / 10_000
                else:
                    excess = 0.0
            result = dict(id=episode["id"], arm=arm.name, observed_at=frame.received_at.isoformat(),
                          source_frame=frame.id, due_at=episode["due_at"], symbol=symbol,
                          action=episode["action"], net_markout=excess,
                          measurement_lag_seconds=(frame.received_at-instant(episode["due_at"])).total_seconds(),
                          p_up=episode["p_up"], status="MATURED_LABEL_NOT_EXECUTION",
                          measure="SIGNED_COUNTERFACTUAL_MARKOUT_NOT_TRADE_PNL")
            arm.matured.append(result)
            episode["status"] = "MATURED"
            self.ledger.append("matured_label", result)

    def _settle(self, arm: Arm, frame: Frame) -> None:
        order = arm.pending
        if order is None or frame.received_at < instant(order["eligible_at"]):
            return
        arm.pending = None
        quote = frame.quotes[order["symbol"]]
        age = (frame.received_at - quote.exchange_at).total_seconds()
        error = None
        if frame.id == order["source_frame"] or frame.received_at <= instant(order["decision_at"]):
            error = "SAME_OR_PRE_DECISION_RECEIPT"
        elif not 0 <= age <= self.protocol.max_quote_age_seconds:
            error = "STALE_QUOTE"
        elif quote.spread_bps > self.protocol.max_spread_bps:
            error = "SPREAD_LIMIT"
        elif arm.halted and order["action"] == "BUY":
            error = "DRAWDOWN_HALT"
        else:
            equity, marked = self._equity(arm, frame)
            fraction = min(order["fraction"], self.protocol.max_position_fraction)
            if order["action"] == "BUY":
                if order["symbol"] in arm.positions:
                    error = "POSITION_ALREADY_OPEN"
                else:
                    price = quote.ask * (1 + self.protocol.slippage_bps_per_side / 10_000)
                    notional = min(fraction * equity, arm.cash / (1 + self.protocol.fee_bps_per_side / 10_000))
                    if marked + notional > equity * self.protocol.max_total_exposure_fraction:
                        error = "TOTAL_EXPOSURE_LIMIT"
                    elif notional < self.protocol.min_notional_usdt:
                        error = "MIN_NOTIONAL"
                    elif notional / price > quote.ask_size * self.protocol.max_book_participation:
                        error = "INSUFFICIENT_VISIBLE_ASK"
                    else:
                        qty = notional / price
                        fee = notional * self.protocol.fee_bps_per_side / 10_000
                        arm.cash -= notional + fee
                        arm.positions[order["symbol"]] = Position(qty, notional + fee)
            else:
                position = arm.positions.get(order["symbol"])
                if position is None:
                    error = "NO_POSITION_TO_SELL"
                else:
                    price = quote.bid * (1 - self.protocol.slippage_bps_per_side / 10_000)
                    qty = position.quantity * order["fraction"]
                    if qty * price < self.protocol.min_notional_usdt:
                        error = "MIN_NOTIONAL"
                    elif qty > quote.bid_size * self.protocol.max_book_participation:
                        error = "INSUFFICIENT_VISIBLE_BID"
                    else:
                        gross = qty * price
                        fee = gross * self.protocol.fee_bps_per_side / 10_000
                        basis = position.cost_basis * (qty / position.quantity)
                        arm.cash += gross - fee
                        arm.realized_pnl += gross - fee - basis
                        position.quantity -= qty
                        position.cost_basis -= basis
                        if position.quantity < 1e-10:
                            del arm.positions[order["symbol"]]
        if error:
            arm.rejected_orders += 1
            self.ledger.append("order_rejected", dict(arm=arm.name, order=order,
                              receipt_frame=frame.id, reason=error))
        else:
            arm.fills += 1
            arm.fees_paid += fee
            arm.slippage_paid += qty * (quote.ask if order["action"] == "BUY" else quote.bid) * (
                self.protocol.slippage_bps_per_side / 10_000)
            arm.gross_turnover += price * qty
            self.ledger.append("paper_fill", dict(arm=arm.name, order=order,
                              quote_frame=frame.id, quote_received_at=frame.received_at.isoformat(),
                              exchange_quote_at=quote.exchange_at.isoformat(),
                              execution_price=price, quantity=qty,
                              fee=fee, cash_after=arm.cash, realized_pnl=arm.realized_pnl,
                              type="CONDITIONAL_VISIBLE_DEPTH_PAPER"))

    def observe(self, raw: dict[str, Any]) -> None:
        frame = Frame.parse(raw)
        if frame.id in self._seen or (self._last and frame.received_at <= self._last.received_at):
            raise ReplayError("DUPLICATE_OR_BACKDATED_RECEIPT")
        if self._universe is None:
            self._universe = set(frame.quotes)
        elif set(frame.quotes) != self._universe:
            raise ReplayError("CHANGED_UNIVERSE_OR_MISSING_MARK")
        self._seen.add(frame.id)
        self._frames += 1
        self._raw_frames.append(raw)
        self.ledger.append("receipt", dict(id=frame.id, received_at=frame.received_at.isoformat(),
                          raw_sha256=frame.raw_hash, symbols=sorted(frame.quotes),
                          raw=raw))
        for arm in self.arms.values():
            self._settle(arm, frame)
            self._mature(arm, frame)
            equity, _ = self._equity(arm, frame)
            arm.peak_equity = max(equity, arm.peak_equity)
            arm.worst_drawdown = max(arm.worst_drawdown, 1 - equity / arm.peak_equity)
            if equity / arm.peak_equity <= 1 - self.protocol.max_drawdown_fraction:
                arm.halted = True
        self._evaluate(frame)
        self._last = frame

    def _evaluate(self, frame: Frame) -> None:
        previous = self._last
        history = {s: {"previous_mid": previous.quotes[s].mid if previous else None,
                       "current_bid": q.bid, "current_ask": q.ask, "current_mid": q.mid,
                       "current_spread_bps": q.spread_bps,
                       "quote_exchange_at": q.exchange_at.isoformat()}
                   for s, q in frame.quotes.items()}
        for arm in self.arms.values():
            equity, _ = self._equity(arm, frame)
            memory = [dict(x) for x in arm.matured if instant(x["observed_at"]) <= frame.received_at]
            context = dict(frame_id=frame.id, cutoff=frame.received_at.isoformat(), history=history,
                           universe=sorted(frame.quotes), cash=arm.cash, equity=equity,
                           positions={s: {"quantity": p.quantity, "cost_basis": p.cost_basis}
                                      for s, p in arm.positions.items()},
                           pending_order=bool(arm.pending), halted=arm.halted,
                           resolved_memory=memory[-30:], protocol=dict(vars(self.protocol)))
            start = time.monotonic()
            try:
                raw = arm.planner.propose(context)
                latency = time.monotonic() - start
                if raw is None:
                    raise ReplayError("MODEL_UNAVAILABLE_NOT_CASH")
                plan = Plan.parse(raw, self._universe or set())
            except Exception as exc:
                arm.model_unavailable += 1
                self.ledger.append("decision_unavailable", dict(arm=arm.name, frame_id=frame.id,
                    context_hash=sha256(context), reason=str(exc)[:150],
                    elapsed_seconds=time.monotonic()-start))
                continue
            ready = frame.received_at + timedelta(seconds=latency)
            decision_id = sha256([arm.name, frame.id, sha256(raw)])
            event = dict(id=decision_id, arm=arm.name, source_frame=frame.id,
                         observed_at=frame.received_at.isoformat(), ready_at=ready.isoformat(),
                         model_latency_seconds=latency, context_hash=sha256(context),
                         memory_ids=[m["id"] for m in memory[-30:]], plan=raw)
            self.ledger.append("decision", event)
            arm.decisions += 1
            if arm.last_episode_due is None or frame.received_at >= arm.last_episode_due:
                due = frame.received_at + timedelta(seconds=self.protocol.label_horizon_seconds)
                arm.episodes.append(dict(id=decision_id, symbol=plan.symbol, action=plan.action,
                                         status="PENDING", origin_mid=frame.quotes[plan.symbol].mid
                                         if plan.symbol != "CASH" else None,
                                         p_up=plan.p_up, due_at=due.isoformat()))
                arm.last_episode_due = due
            if plan.action != "HOLD" and arm.pending is None:
                if not (arm.halted and plan.action == "BUY"):
                    arm.pending = dict(decision_id=decision_id, action=plan.action,
                        symbol=plan.symbol, fraction=plan.fraction, source_frame=frame.id,
                        decision_at=ready.isoformat(),
                        eligible_at=(ready+timedelta(seconds=self.protocol.execution_delay_seconds)).isoformat())
                    self.ledger.append("order_intent", dict(arm=arm.name, **arm.pending))

    def finish(self) -> dict[str, Any]:
        if self._frames < 2 or self._last is None:
            raise ReplayError("INSUFFICIENT_REPLAY")
        summary = {}
        for arm in self.arms.values():
            equity, exposure = self._equity(arm, self._last)
            summary[arm.name] = dict(initial_cash=self.protocol.capital_usdt, cash=arm.cash,
                equity_marked_at_bid=equity, pnl_marked_at_bid=equity-self.protocol.capital_usdt,
                return_fraction=equity/self.protocol.capital_usdt-1, realized_pnl=arm.realized_pnl,
                open_exposure_marked_at_bid=exposure, open_positions=len(arm.positions),
                pending_order=arm.pending is not None, fills=arm.fills,
                decisions=arm.decisions, fees_paid=arm.fees_paid,
                modeled_slippage_paid=arm.slippage_paid,
                gross_turnover_usdt=arm.gross_turnover,
                max_observed_drawdown_fraction=arm.worst_drawdown,
                rejected_orders=arm.rejected_orders, model_unavailable=arm.model_unavailable,
                matured_independent_labels=len(arm.matured),
                pending_labels=sum(x["status"]=="PENDING" for x in arm.episodes),
                drawdown_halted=arm.halted)
        return dict(version="ONE_AI_NATIVE_RESEARCH_V1", data_use=self.data_use,
                    evidence_class="OFFLINE_RECONSTRUCTION_NOT_FORWARD_ALPHA",
                    live_trading_enabled=False,
                    input_frames=self._frames, initial_capital_per_arm=self.protocol.capital_usdt,
                    separate_comparison_sleeves_not_aggregate_capital=True,
                    protocol=dict(vars(self.protocol)), summary=summary,
                    last_receipt=self._last.received_at.isoformat(),
                    ledger_tip=self.ledger.tip, ledger=self.ledger.records)
