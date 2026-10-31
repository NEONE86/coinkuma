"""Independent audit of an AI-native PAPER replay result.

Do not import the experiment implementation: recalculate the hash chain,
time ordering, decisions, quote-based fills and final marked cash accounts.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any


class AuditError(ValueError):
    pass


def fail(reason: str) -> None:
    raise AuditError(reason)


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def at(value: Any) -> datetime:
    if not isinstance(value, str):
        fail("AUDIT_TIME_TYPE")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("AUDIT_TIME_PARSE") from exc
    if timestamp.tzinfo is None:
        fail("AUDIT_NAIVE_TIME")
    return timestamp.astimezone(timezone.utc)


def approx(a: Any, b: Any, *, label: str) -> None:
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           for x in (a, b)) or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8):
        fail("ACCOUNT_MISMATCH:" + label)


def check(test: bool, reason: str) -> None:
    if not test:
        fail(reason)


def verify(record: dict[str, Any]) -> dict[str, Any]:
    """Raise AuditError on a failed invariant; return audit counts on success."""
    check(record.get("version") == "ONE_AI_NATIVE_RESEARCH_V1", "UNKNOWN_VERSION")
    check(record.get("data_use") in ("SYNTHETIC", "REUSED_HISTORICAL", "PROSPECTIVE_REPLAY"),
          "DATA_USE_UNDECLARED")
    check(record.get("live_trading_enabled") is False, "LIVE_MODE_NOT_AUDITABLE")
    check(record.get("evidence_class") == "OFFLINE_RECONSTRUCTION_NOT_FORWARD_ALPHA", "EVIDENCE_CLASS")
    protocol = record["protocol"]
    cash_start = protocol["capital_usdt"]
    fee_rate = protocol["fee_bps_per_side"] / 10_000
    slippage = protocol["slippage_bps_per_side"] / 10_000
    arms = {name: {"cash": cash_start, "positions": {}, "realized": 0.0,
                   "fills": 0, "rejections": 0, "unavailable": 0,
                   "decisions": {}, "pending": None, "episodes": {}, "matured": [],
                   "fees": 0.0, "slippage": 0.0, "turnover": 0.0,
                   "peak": cash_start, "worst_drawdown": 0.0}
            for name in record["summary"]}
    check("CASH" in arms, "NO_CASH_CONTROL")
    ledger = record["ledger"]
    check(isinstance(ledger, list) and bool(ledger), "EMPTY_LEDGER")
    tip = "0" * 64
    last = None
    receipt = None
    seen = set()
    symbols = None
    counts = {"receipts": 0, "decisions": 0, "fills": 0, "rejected": 0,
              "matured_labels": 0, "unavailable": 0}
    def track_equity(mark_frame: dict[str, Any]) -> None:
        for arm in arms.values():
            exposure = sum(pos["quantity"] * mark_frame["quotes"][symbol]["bid"]
                           for symbol, pos in arm["positions"].items())
            marked = arm["cash"] + exposure
            arm["peak"] = max(marked, arm["peak"])
            arm["worst_drawdown"] = max(arm["worst_drawdown"], 1 - marked / arm["peak"])
    for seq, row in enumerate(ledger):
        check(row.get("seq") == seq and row.get("previous_hash") == tip,
              "BROKEN_APPEND_ONLY_CHAIN")
        check(row.get("hash") == digest({key: value for key, value in row.items() if key != "hash"}),
              "INVALID_RECORD_HASH")
        tip = row["hash"]
        kind, entry = row["kind"], row["payload"]
        if kind == "receipt":
            if receipt is not None:
                track_equity(receipt)
            raw = entry["raw"]
            check(isinstance(raw, dict) and set(raw) == {"id", "received_at", "quotes"},
                  "RECEIPT_SCHEMA")
            check(digest(raw) == entry["raw_sha256"], "RAW_BYTES_HASH_MISMATCH")
            check(entry["id"] == raw["id"] and entry["received_at"] == at(raw["received_at"]).isoformat(),
                  "RECEIPT_PROJECTION")
            current = at(raw["received_at"])
            check(raw["id"] not in seen and (last is None or last < current),
                  "DUPLICATE_OR_BACKDATED_RECEIPT")
            seen.add(raw["id"])
            last = current
            receipt = raw
            check(isinstance(raw["quotes"], dict) and raw["quotes"], "NO_QUOTES")
            check(entry["symbols"] == sorted(raw["quotes"]), "SYMBOL_PROJECTION")
            if symbols is None:
                symbols = set(raw["quotes"])
            check(set(raw["quotes"]) == symbols, "UNIVERSE_CHANGED")
            for quote in raw["quotes"].values():
                check(set(quote) == {"bid", "ask", "bid_size", "ask_size", "exchange_at"},
                      "QUOTE_FIELDS")
                check(all(isinstance(quote[k], (int, float)) and not isinstance(quote[k], bool)
                          and math.isfinite(quote[k]) and quote[k] > 0
                          for k in ("bid", "ask", "bid_size", "ask_size")), "QUOTE_NUMBER")
                check(quote["bid"] < quote["ask"] and at(quote["exchange_at"]) <= current,
                      "CROSSED_OR_FUTURE_QUOTE")
            counts["receipts"] += 1
            continue
        check(receipt is not None, "EVENT_WITHOUT_RECEIPT")
        arm = arms.get(entry.get("arm"))
        check(arm is not None, "UNKNOWN_ARM")
        if kind == "decision_unavailable":
            check(entry["frame_id"] == receipt["id"], "UNAVAILABLE_FRAME_MISMATCH")
            arm["unavailable"] += 1
            counts["unavailable"] += 1
            continue
        if kind == "decision":
            check(entry["source_frame"] == receipt["id"] and
                  at(entry["observed_at"]) == last and at(entry["ready_at"]) >= last,
                  "LOOKAHEAD_DECISION")
            check(entry["id"] not in arm["decisions"] and
                  len([x for x in arm["decisions"].values() if x["source_frame"] == receipt["id"]]) == 0,
                  "DUPLICATE_DECISION")
            plan = entry["plan"]
            check(set(plan) == {"action", "symbol", "fraction", "p_up", "thesis",
                                 "counter_hypothesis", "invalidation"}, "PLAN_SCHEMA")
            check(plan["action"] in ("BUY", "SELL", "HOLD") and
                  plan["symbol"] in (symbols | {"CASH"}) and
                  isinstance(plan["fraction"], (int, float)) and 0 <= plan["fraction"] <= 1 and
                  isinstance(plan["p_up"], (int, float)) and 0 <= plan["p_up"] <= 1,
                  "PLAN_VALUE")
            check((plan["action"] == "HOLD") == (plan["fraction"] == 0), "PLAN_SIZE")
            check(entry["memory_ids"] == [m["id"] for m in arm["matured"]][-30:],
                  "FUTURE_OR_CROSS_ARM_MEMORY")
            arm["decisions"][entry["id"]] = entry
            counts["decisions"] += 1
            continue
        if kind == "order_intent":
            check(entry["source_frame"] == receipt["id"] and
                  arm["pending"] is None, "ORDER_INTENT_WITHOUT_NEW_DECISION")
            decision = arm["decisions"].get(entry["decision_id"])
            check(decision is not None and decision["source_frame"] == receipt["id"],
                  "ORDER_NO_DECISION")
            plan = decision["plan"]
            check(plan["action"] in ("BUY", "SELL") and
                  (entry["action"], entry["symbol"], entry["fraction"]) ==
                  (plan["action"], plan["symbol"], plan["fraction"]), "ORDER_PLAN_MISMATCH")
            check(at(entry["decision_at"]) == at(decision["ready_at"]) and
                  at(entry["eligible_at"]) ==
                  at(entry["decision_at"]) + timedelta(seconds=protocol["execution_delay_seconds"]),
                  "ORDER_ELIGIBILITY")
            arm["pending"] = entry.copy()
            arm["pending"].pop("arm", None)
            continue
        if kind in ("paper_fill", "order_rejected"):
            order = entry["order"]
            check(arm["pending"] == order and order["source_frame"] != receipt["id"] and
                  at(order["decision_at"]) < last and at(order["eligible_at"]) <= last,
                  "SAME_FRAME_OR_EARLY_EXECUTION")
            arm["pending"] = None
            quote = receipt["quotes"][order["symbol"]]
            if kind == "order_rejected":
                arm["rejections"] += 1
                counts["rejected"] += 1
                continue
            check(entry["quote_frame"] == receipt["id"] and
                  at(entry["quote_received_at"]) == last and
                  at(entry["exchange_quote_at"]) == at(quote["exchange_at"]),
                  "FILL_QUOTE_PROJECTION")
            age = (last - at(quote["exchange_at"])).total_seconds()
            spread = (quote["ask"] - quote["bid"]) / ((quote["ask"] + quote["bid"]) / 2) * 10_000
            check(0 <= age <= protocol["max_quote_age_seconds"] and
                  spread <= protocol["max_spread_bps"], "FILL_STALE_OR_WIDE_QUOTE")
            mark = sum(position["quantity"] * receipt["quotes"][s]["bid"]
                       for s, position in arm["positions"].items())
            equity = arm["cash"] + mark
            fraction = min(order["fraction"], protocol["max_position_fraction"])
            if order["action"] == "BUY":
                check(order["symbol"] not in arm["positions"], "DUPLICATE_BUY")
                price = quote["ask"] * (1 + slippage)
                notional = min(fraction * equity, arm["cash"] / (1 + fee_rate))
                check(mark + notional <= equity * protocol["max_total_exposure_fraction"] and
                      notional >= protocol["min_notional_usdt"], "FILL_RISK_LIMIT")
                quantity = notional / price
                check(quantity <= quote["ask_size"] * protocol["max_book_participation"],
                      "FILL_VISIBLE_DEPTH")
                charge = notional * fee_rate
                arm["cash"] -= notional + charge
                arm["positions"][order["symbol"]] = {"quantity": quantity, "basis": notional + charge}
            else:
                position = arm["positions"].get(order["symbol"])
                check(position is not None, "SELL_WITHOUT_POSITION")
                price = quote["bid"] * (1 - slippage)
                quantity = position["quantity"] * order["fraction"]
                check(quantity * price >= protocol["min_notional_usdt"] and
                      quantity <= quote["bid_size"] * protocol["max_book_participation"],
                      "SELL_RISK_OR_DEPTH")
                gross = quantity * price
                charge = gross * fee_rate
                allocated_basis = position["basis"] * (quantity / position["quantity"])
                arm["cash"] += gross - charge
                arm["realized"] += gross - charge - allocated_basis
                position["quantity"] -= quantity
                position["basis"] -= allocated_basis
                if position["quantity"] < 1e-10:
                    del arm["positions"][order["symbol"]]
            approx(entry["execution_price"], price, label="execution_price")
            approx(entry["quantity"], quantity, label="quantity")
            approx(entry["fee"], charge, label="fee")
            approx(entry["cash_after"], arm["cash"], label="cash")
            approx(entry["realized_pnl"], arm["realized"], label="realized")
            arm["fills"] += 1
            arm["fees"] += charge
            arm["slippage"] += quantity * (
                quote["ask"] if order["action"] == "BUY" else quote["bid"]) * slippage
            arm["turnover"] += price * quantity
            counts["fills"] += 1
            continue
        if kind == "matured_label":
            check(entry["source_frame"] == receipt["id"] and
                  at(entry["observed_at"]) == last and last >= at(entry["due_at"]),
                  "PREMATURE_LABEL")
            episode = arm["decisions"].get(entry["id"])
            check(episode is not None and entry["id"] not in arm["episodes"],
                  "LABEL_WITHOUT_DECISION_OR_DUPLICATE")
            due = at(episode["observed_at"]) + timedelta(seconds=protocol["label_horizon_seconds"])
            check(due == at(entry["due_at"]) and entry["action"] == episode["plan"]["action"] and
                  entry["symbol"] == episode["plan"]["symbol"], "LABEL_ORIGIN")
            # Origin prices are not assumed from the current packet: find the decision's receipt.
            origin = next((item["payload"]["raw"] for item in ledger[:seq]
                           if item["kind"] == "receipt" and
                           item["payload"]["id"] == episode["source_frame"]), None)
            check(origin is not None, "LABEL_ORIGIN_FRAME_NOT_FOUND")
            if entry["action"] == "HOLD" or entry["symbol"] == "CASH":
                expected = 0.0
            else:
                old = origin["quotes"][entry["symbol"]]
                new = receipt["quotes"][entry["symbol"]]
                age = (last - at(new["exchange_at"])).total_seconds()
                check(0 <= age <= protocol["max_quote_age_seconds"], "STALE_MATURE_LABEL")
                raw_ret = (new["bid"] + new["ask"]) / (old["bid"] + old["ask"]) - 1
                expected = (1 if entry["action"] == "BUY" else -1) * raw_ret - 2 * (fee_rate + slippage)
            approx(entry["net_markout"], expected, label="mature_markout")
            arm["episodes"][entry["id"]] = entry
            arm["matured"].append(entry)
            counts["matured_labels"] += 1
            continue
        fail("UNKNOWN_LEDGER_EVENT:" + str(kind))
    check(tip == record["ledger_tip"] and counts["receipts"] == record["input_frames"],
          "LEDGER_TIP_OR_RECEIPT_COUNT")
    check(at(record["last_receipt"]) == last, "FINAL_RECEIPT_MISMATCH")
    track_equity(receipt)
    for name, arm in arms.items():
        report = record["summary"][name]
        exposure = sum(pos["quantity"] * receipt["quotes"][symbol]["bid"]
                       for symbol, pos in arm["positions"].items())
        equity = arm["cash"] + exposure
        for key, amount in (("cash", arm["cash"]), ("equity_marked_at_bid", equity),
                            ("open_exposure_marked_at_bid", exposure),
                            ("pnl_marked_at_bid", equity - cash_start),
                            ("return_fraction", equity / cash_start - 1),
                            ("realized_pnl", arm["realized"]),
                            ("fees_paid", arm["fees"]),
                            ("modeled_slippage_paid", arm["slippage"]),
                            ("gross_turnover_usdt", arm["turnover"]),
                            ("max_observed_drawdown_fraction", arm["worst_drawdown"])):
            approx(report[key], amount, label=name + ":" + key)
        for key, amount in (("fills", arm["fills"]), ("rejected_orders", arm["rejections"]),
                            ("decisions", len(arm["decisions"])),
                            ("model_unavailable", arm["unavailable"]),
                            ("open_positions", len(arm["positions"])),
                            ("matured_independent_labels", len(arm["matured"]))):
            check(report[key] == amount, "SUMMARY_COUNT:" + name + ":" + key)
        check(report["pending_order"] == (arm["pending"] is not None), "PENDING_ORDER_SUMMARY")
    return {"status": "PASS_INDEPENDENT_RECALCULATION", "counts": counts,
            "ledger_tip": tip, "arms": sorted(arms), "not_forward_alpha": True}
