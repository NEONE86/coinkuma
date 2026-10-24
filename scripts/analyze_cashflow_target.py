#!/usr/bin/env python3
"""Deterministic cash-withdrawal stress on observed research return paths.

No forecast, account access, market-data generation, or parameter optimization.
The KRW amounts only scale USDT-denominated returns; FX and taxes are excluded.
"""
from __future__ import annotations

import argparse
import calendar
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics


DAY = 86_400_000


def date_of(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).date()


def product(values):
    out = 1.0
    for value in values:
        out *= value
    return out


def month_id(date):
    return f"{date.year:04d}-{date.month:02d}"


def quantile(values, p):
    if not values:
        return None
    vals = sorted(values)
    i = (len(vals) - 1) * p
    lo = math.floor(i)
    hi = math.ceil(i)
    return vals[lo] * (hi - i) + vals[hi] * (i - lo) if hi != lo else vals[lo]


def monthly_rows(run):
    """Recompute full calendar-month returns from raw daily closing equity."""
    initial = float(run["config"]["initial_cash"])
    equity = run["equity"]
    if not equity or initial <= 0:
        raise ValueError("positive initial capital and daily equity required")
    buckets = {}
    prior = initial
    last_day = None
    for row in equity:
        date = date_of(row["ts_ms"])
        if last_day is not None and (date - last_day).days != 1:
            raise ValueError("daily calendar is not consecutive")
        value = float(row["equity"])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("equity must be positive and finite")
        key = month_id(date)
        b = buckets.setdefault(key, {"month": key, "begin_equity": prior,
            "first_date": date.isoformat(), "last_date": date.isoformat(),
            "days": 0, "daily_factors": [], "end_equity": None,
            "end_cash": None, "realized_trade_pnl": 0.0, "writeoff_pnl": 0.0,
            "original_halted_days": 0})
        b["last_date"] = date.isoformat()
        b["days"] += 1
        b["daily_factors"].append(value / prior)
        b["end_equity"] = value
        b["end_cash"] = row.get("cash")
        b["original_halted_days"] += bool(row.get("halted", False))
        prior = value
        last_day = date
    for fill in run.get("fills", []):
        pnl = fill.get("realized_pnl")
        if pnl is not None:
            key = month_id(date_of(fill["ts_ms"]))
            if key in buckets:
                buckets[key]["realized_trade_pnl"] += float(pnl)
    for loss in run.get("writeoffs", []):
        key = month_id(date_of(loss["ts_ms"]))
        if key in buckets:
            buckets[key]["writeoff_pnl"] -= float(loss["cost_basis_lost"])
    out = []
    for b in buckets.values():
        first = datetime.fromisoformat(b["first_date"]).date()
        days = calendar.monthrange(first.year, first.month)[1]
        b["full_calendar_month"] = first.day == 1 and b["days"] == days
        b["equity_net_return"] = b["end_equity"] / b["begin_equity"] - 1
        b["realized_pnl_including_writeoffs"] = b["realized_trade_pnl"] + b["writeoff_pnl"]
        out.append(b)
    return out


def withdrawal_path(months, initial_krw, withdrawal_krw):
    """Apply frozen monthly net return factors, then end-month withdrawals.

    Proportional-return overlay: not a strategy rerun at smaller capital. It
    deliberately does not invent liquidation, tax, or extra trading proceeds.
    A withdrawal may require selling positions, whose extra cost is omitted.
    """
    capital = initial_krw
    paid = 0.0
    full_payments = 0
    first_shortfall = None
    depleted = None
    trading_index = peak = 1.0
    mdd = 0.0
    trigger_month = None
    path = []
    for i, month in enumerate(months, 1):
        before = capital
        for factor in month["daily_factors"]:
            capital *= factor
            trading_index *= factor
            peak = max(peak, trading_index)
            mdd = min(mdd, trading_index / peak - 1)
            if mdd <= -0.15 and trigger_month is None:
                trigger_month = i
        available = max(0.0, capital)
        payment = min(available, withdrawal_krw)
        shortfall = withdrawal_krw - payment
        if shortfall <= 1e-6:
            full_payments += 1
        elif first_shortfall is None:
            first_shortfall = i
        paid += payment
        capital = max(0.0, available - payment)
        if capital <= 1e-6 and depleted is None:
            depleted = i
        path.append({"month": month["month"], "begin_capital_krw": before,
            "after_return_before_withdrawal_krw": available, "paid_krw": payment,
            "shortfall_krw": shortfall, "end_capital_krw": capital})
    return {"start_month": months[0]["month"], "horizon_months": len(months),
        "fully_paid_months": full_payments, "first_shortfall_month_number": first_shortfall,
        "depleted_month_number": depleted, "total_paid_krw": paid,
        "ending_capital_krw": capital, "funded_all_with_positive_remaining_capital":
            first_shortfall is None and capital > 1e-6,
        "return_path_drawdown_excluding_withdrawals": mdd,
        "return_path_15pct_drawdown_first_month_number": trigger_month,
        "monthly_path": path}


def withdrawal_windows(months, initial, withdrawal, horizon=12):
    runs = [withdrawal_path(months[i:i+horizon], initial, withdrawal)
            for i in range(max(0, len(months)-horizon+1))]
    capital_requirements = []
    for i in range(len(runs)):
        factor = 1.0
        discounted_payments = 0.0
        for month in months[i:i+horizon]:
            factor *= product(month["daily_factors"])
            discounted_payments += withdrawal / factor
        capital_requirements.append({"start_month":months[i]["month"],
            "minimum_initial_capital_to_fund_window_ending_at_zero_krw":discounted_payments,
            "initial_capital_to_fund_window_and_preserve_initial_principal_krw":
                factor*discounted_payments/(factor-1) if factor>1 else None})
    n = len(runs)
    depleted = [r["depleted_month_number"] for r in runs if r["depleted_month_number"] is not None]
    preserve = [r["initial_capital_to_fund_window_and_preserve_initial_principal_krw"]
                for r in capital_requirements if r["initial_capital_to_fund_window_and_preserve_initial_principal_krw"] is not None]
    return {"withdrawal_krw_per_month": withdrawal, "initial_capital_krw": initial,
        "window_months": horizon, "historical_window_count": n,
        "fully_funded_with_capital_remaining_count": sum(r["funded_all_with_positive_remaining_capital"] for r in runs),
        "fully_funded_with_capital_remaining_fraction": sum(r["funded_all_with_positive_remaining_capital"] for r in runs)/n if n else None,
        "depleted_count": len(depleted),
        "depletion_month_min": min(depleted) if depleted else None,
        "depletion_month_median": statistics.median(depleted) if depleted else None,
        "depletion_month_max": max(depleted) if depleted else None,
        "return_path_15pct_drawdown_window_count": sum(r["return_path_15pct_drawdown_first_month_number"] is not None for r in runs),
        "original_halt_overlap_window_count": sum(any(m.get("original_halted_days",0)>0 for m in months[i:i+horizon]) for i in range(n)),
        "ending_capital_krw_p10": quantile([r["ending_capital_krw"] for r in runs], .1),
        "ending_capital_krw_median": quantile([r["ending_capital_krw"] for r in runs], .5),
        "ending_capital_krw_p90": quantile([r["ending_capital_krw"] for r in runs], .9),
        "windows_where_no_finite_capital_preserves_principal": n-len(preserve),
        "principal_preserving_capital_median_on_positive_return_windows_krw": quantile(preserve,.5),
        "capital_requirement_interpretation": "Frozen-path algebra, not recommended capital. Losing/flat windows cannot pay withdrawals while preserving principal at any positive capital.",
        "capital_requirements": capital_requirements,
        "windows": runs}


def analyze(run):
    raw_months = monthly_rows(run)
    months = [r for r in raw_months if r["full_calendar_month"]]
    if not months:
        raise ValueError("no full calendar month available")
    # Avoid silently joining separated complete-month segments.
    for a, b in zip(months, months[1:]):
        ay, am = map(int, a["month"].split("-"))
        by, bm = map(int, b["month"].split("-"))
        if by * 12 + bm != ay * 12 + am + 1:
            raise ValueError("full months are not consecutive")
    returns = [r["equity_net_return"] for r in months]
    median = statistics.median(returns)
    rolling12 = [{"start_month": months[i]["month"], "end_month": months[i+11]["month"],
        "net_return": product(1+r for r in returns[i:i+12])-1}
        for i in range(max(0,len(months)-11))]
    post2024 = [r for m,r in zip(months,returns) if m["month"] >= "2024-01"]
    after2024 = product(1+r for r in post2024)-1 if post2024 else None
    summary = run.get("summary", {})
    halted_days = [r for r in run["equity"] if r.get("halted",False)]
    pf = summary.get("profit_factor")
    exposure = summary.get("average_daily_exposure")
    if (pf is not None and pf <= 1.2) or summary.get("total_return",0) <= 0:
        next_test = "edge_discrimination_before_capital_scaling"
    elif summary.get("trade_count",0) < 100 or (exposure is not None and exposure < .15):
        next_test = "opportunity_and_capital_efficiency_with_fixed_risk"
    else:
        next_test = "forward_execution_and_cost_validation"
    return {
        "scope": "Historical deterministic return-path analysis, not future probability or a new withdrawal backtest of the execution engine",
        "cashflow_math": {"initial_capital_krw": 20000000, "per_trade_risk_fraction": .005,
            "minimum_target_krw_per_month": 5000000, "upper_target_krw_per_month": 10000000,
            "required_simple_monthly_return_at_unchanged_capital": [.25, .50],
            "required_monthly_initial_equity_risk_units": [50,100]},
        "monthly_observations": {"full_calendar_months": len(months),
            "first": months[0]["month"], "last": months[-1]["month"],
            "net_return_mean": statistics.mean(returns), "net_return_median": median,
            "net_return_min": min(returns), "net_return_max": max(returns),
            "positive_months": sum(r>0 for r in returns),
            "positive_month_fraction": sum(r>0 for r in returns)/len(returns),
            "months_at_least_25pct": sum(r>=.25 for r in returns),
            "months_at_least_50pct": sum(r>=.5 for r in returns),
            "median_return_scaled_to_20m_krw": median * 20000000,
            "best_month_return_scaled_to_20m_krw": max(returns) * 20000000,
            "worst_month_return_scaled_to_20m_krw": min(returns) * 20000000,
            "post2024_total_return": after2024,
            "rolling12_worst_return": min(r["net_return"] for r in rolling12) if rolling12 else None,
            "rolling12_median_return": statistics.median(r["net_return"] for r in rolling12) if rolling12 else None,
            "rolling12_best_return": max(r["net_return"] for r in rolling12) if rolling12 else None},
        "capital_at_historical_median_monthly_return": {
            "for_5m_monthly_krw": 5000000/median if median>0 else None,
            "for_10m_monthly_krw": 10000000/median if median>0 else None,
            "interpretation": "Algebra only; undefined if median <=0. Median is not a dependable withdrawal rate and ignores losing months, tax and FX."},
        "cashflow_gate_checks": {"median_month_at_least_25pct": median>=.25,
            "positive_month_fraction_at_least_75pct": sum(r>0 for r in returns)/len(returns)>=.75,
            "post2024_nonnegative": after2024 is not None and after2024>=0},
        "withdrawal_overlays": [withdrawal_windows(months,20000000,w) for w in (5000000,10000000)],
        "original_run_halt": {"latched_drawdown_halt": summary.get("latched_drawdown_halt"),
            "first_halted_date": date_of(halted_days[0]["ts_ms"]).isoformat() if halted_days else None,
            "halted_daily_rows": len(halted_days),
            "interpretation": "Original strategy halt only. Shifted windows reuse its realized path; they are not fresh-account reruns."},
        "next_experiment_branch": next_test,
        "next_experiment_numeric_gates": {"minimum_completed_campaigns": 200,
            "stress_profit_factor_min_exclusive": 1.3,
            "stress_mean_campaign_pnl_positive": True,
            "block_resampled_lower95pct_mean_monthly_return_min_exclusive": 0,
            "calendar_fold_positive_fraction_min": .60,
            "drawdown_trigger": .15,
            "forward_paper_min_days": 90,
            "leverage_until_evidence": 1.0,
            "selection_rule": "These are next-test design gates, not a claim v1 meets them. Do not retune v1 or substitute a leveraged spot-return multiple."},
        "limitations": ["KRW merely rescales USDT percentage returns; no FX path, taxes, operational expenses or remittance costs",
            "Equity returns include unrealized PnL; realized_trade_pnl columns are separately reported",
            "Withdrawal overlay preserves percentage returns and omits additional sales/costs/minimum-order effects; therefore is not execution-valid",
            "Overlapping historical windows are dependent observations, never estimates of future success probability",
            "Drawdowns exclude withdrawals; an overlay trigger is diagnostic and does not fabricate a new strategy liquidation"],
        "monthly_rows": [{k:v for k,v in m.items() if k!="daily_factors"} for m in raw_months],
        "rolling12": rolling12,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.run)
    raw = source.read_bytes()
    out = analyze(json.loads(raw))
    out["input_sha256"] = hashlib.sha256(raw).hexdigest()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    print(json.dumps({"monthly_observations": out["monthly_observations"],
        "cashflow_gate_checks": out["cashflow_gate_checks"],
        "withdrawal_overlays": [{k:v for k,v in r.items() if k not in {"windows","capital_requirements"}} for r in out["withdrawal_overlays"]],
        "next_experiment_branch": out["next_experiment_branch"]}, indent=2))


if __name__ == "__main__":
    main()
