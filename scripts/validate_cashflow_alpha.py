"""Independent ledger/metric audit for Cashflow Alpha research artifacts.

This module does not import the strategy's metric implementation. Bootstrap
intervals are historical block-resampling diagnostics, not future probabilities.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import statistics
import sys

DAY = 86_400_000


def date_key(ts, fmt):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime(fmt)


def close(a, b, name, scale=1.0):
    if not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8 * max(1.0, scale)):
        raise AssertionError(f"{name}: {a} != {b}")


def quantile(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def drawdown(values, initial):
    peak, worst = initial, 0.0
    for value in values:
        if not math.isfinite(value) or value <= 0:
            raise AssertionError("nonpositive_or_nonfinite_equity")
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def ledger_campaigns(result):
    """Reconstruct flat-to-flat campaigns independently, including writeoffs."""
    initial = result["config"]["initial_cash"]
    cash = initial
    positions = {}
    campaigns = []
    events = [(f["ts_ms"], i, "fill", f) for i, f in enumerate(result["fills"])]
    events += [(w["ts_ms"], len(events) + i, "writeoff", w)
               for i, w in enumerate(result.get("writeoffs", []))]
    fee_total = 0.0
    for ts, _, kind, event in sorted(events):
        symbol = event["symbol"]
        if kind == "writeoff":
            p = positions.pop(symbol)
            close(p["qty"], event["quantity"], "writeoff_quantity")
            p.update(closed_ms=ts, pnl=p["proceeds"]-p["cost"], exit_reason="WRITEOFF")
            campaigns.append(p)
            continue
        qty, fee, price = event["quantity"], event["fee"], event["fill_price"]
        if not all(math.isfinite(x) for x in (qty, fee, price)) or qty <= 0 or fee < 0 or price <= 0:
            raise AssertionError("invalid_fill")
        notional = qty * price
        close(notional, event["notional"], "fill_notional", initial)
        fee_total += fee
        if event["side"] == "BUY":
            p = positions.setdefault(symbol, {"symbol":symbol,"opened_ms":ts,
                "qty":0.0,"cost":0.0,"proceeds":0.0,"buy_fills":0,"sell_fills":0})
            p["qty"] += qty
            p["cost"] += notional + fee
            p["buy_fills"] += 1
            cash -= notional + fee
        elif event["side"] == "SELL":
            p = positions[symbol]
            if qty > p["qty"] + 1e-8 * max(1.0, p["qty"]):
                raise AssertionError("sell_exceeds_inventory")
            p["qty"] -= qty
            p["proceeds"] += notional - fee
            p["sell_fills"] += 1
            cash += notional - fee
            if abs(p["qty"]) <= 1e-8 * max(1.0, qty):
                positions.pop(symbol)
                p.update(closed_ms=ts,pnl=p["proceeds"]-p["cost"],exit_reason=event.get("reason"))
                campaigns.append(p)
        else:
            raise AssertionError("unknown_fill_side")
        if cash < -1e-7 * initial:
            raise AssertionError("negative_spot_cash")
    final = result["equity"][-1]
    close(cash, final["cash"], "final_cash", initial)
    if positions:
        raise AssertionError("final_liquidation_missing")
    close(cash, final["equity"], "final_equity", initial)
    close(sum(p["pnl"] for p in campaigns), cash-initial, "campaign_cash_pnl", initial)
    return campaigns, {"cash_reconciles":True,"inventory_reconciles":True,
                       "campaign_pnl_reconciles":True,"fees":fee_total}


def recompute(result):
    initial = result["config"]["initial_cash"]
    rows = result["equity"]
    if not rows:
        raise AssertionError("empty_equity")
    if any(b["ts_ms"] <= a["ts_ms"] for a,b in zip(rows,rows[1:])):
        raise AssertionError("equity_dates_not_strictly_increasing")
    if any(b["ts_ms"]-a["ts_ms"]!=DAY for a,b in zip(rows,rows[1:])):
        raise AssertionError("daily_equity_calendar_gap")
    campaigns, audit = ledger_campaigns(result)
    annual, monthly = {}, {}
    previous, year_initial, month_initial = initial, {}, {}
    for r in rows:
        y, m = date_key(r["ts_ms"], "%Y"), date_key(r["ts_ms"], "%Y-%m")
        year_initial.setdefault(y, previous)
        month_initial.setdefault(m, previous)
        annual[y] = r["equity"] / year_initial[y] - 1
        monthly[m] = r["equity"] / month_initial[m] - 1
        previous = r["equity"]
    pnl = [p["pnl"] for p in campaigns]
    wins, losses = [p for p in pnl if p>0], [p for p in pnl if p<0]
    years = len(rows)/365.25
    metrics = {
        "total_return":rows[-1]["equity"]/initial-1,
        "cagr":(rows[-1]["equity"]/initial)**(1/years)-1,
        "daily_close_mdd":drawdown([r["equity"] for r in rows],initial),
        "average_daily_exposure":statistics.mean(r.get("exposure_before_end_liquidation",r["exposure"]) for r in rows),
        "closed_campaign_count":len(campaigns), "fill_count":len(result["fills"]),
        "win_rate":len(wins)/len(pnl) if pnl else None,
        "average_win":statistics.mean(wins) if wins else None,
        "average_loss":statistics.mean(losses) if losses else None,
        "payoff_ratio":statistics.mean(wins)/-statistics.mean(losses) if wins and losses else None,
        "profit_factor":sum(wins)/-sum(losses) if losses else None,
        "net_expectancy_per_campaign":statistics.mean(pnl) if pnl else None,
        "annual_returns":annual,"monthly_returns":monthly,
        "median_monthly_return":statistics.median(monthly.values()),
        "positive_month_fraction":sum(v>0 for v in monthly.values())/len(monthly),
        "month_fraction_at_least_25pct":sum(v>=.25 for v in monthly.values())/len(monthly),
        "month_fraction_at_least_50pct":sum(v>=.50 for v in monthly.values())/len(monthly),
    }
    return {"checks":audit,"metrics":metrics,"campaigns":campaigns}


def check_signal_chronology(result):
    plans=result.get("plans",[])
    by_due=defaultdict(list)
    for p in plans:
        signal=p["signal_day_ms"]
        if datetime.fromtimestamp(signal/1000,timezone.utc).weekday()!=6:
            raise AssertionError("non_sunday_signal")
        if p["observed_ms"]!=signal+DAY or p["execute_after_ms"]!=signal+2*DAY:
            raise AssertionError("signal_execution_delay")
        for d in p["decisions"]:
            if d["max_input_close_ms"]>=p["observed_ms"]:
                raise AssertionError("future_signal_input")
        by_due[p["execute_after_ms"]].append(p)
    for f in result["fills"]:
        if f["side"]=="BUY" and not any(
            any(t["symbol"]==f["symbol"] and t["allow_add"] for t in p["targets"])
            for p in by_due[f["ts_ms"]]):
            raise AssertionError("buy_without_prior_delayed_plan")
    return {"signal_plans_checked":len(plans),"plan_chronology":True,"buy_delay":True}


def check_reported_summary(result,independent):
    summary=result["summary"];m=independent["metrics"]
    mapping={"total_return":"total_return","cagr":"cagr","max_drawdown":"daily_close_mdd",
             "average_daily_exposure":"average_daily_exposure","trade_count":"closed_campaign_count",
             "fill_count":"fill_count","win_rate":"win_rate","payoff_ratio":"payoff_ratio",
             "profit_factor":"profit_factor","monthly_median_return":"median_monthly_return",
             "positive_month_fraction":"positive_month_fraction"}
    for reported,computed in mapping.items():
        if summary[reported] is None or m[computed] is None:
            if summary[reported] is not m[computed]:raise AssertionError("summary_null_mismatch:"+reported)
        else:close(summary[reported],m[computed],"summary:"+reported)
    for year,value in m["annual_returns"].items():
        close(value,result["yearly"][year]["return"],"annual_return:"+year)
    close(independent["checks"]["fees"],summary["fees_paid"],"summary_fees")
    return {"reported_summary_matches":True}


def audit_market_fills(result,histories):
    """Daily volume is ex-post execution evidence; never an entry selector here."""
    index={s:{b.open_ms:b for b in bs} for s,bs in histories.items()}
    zero_volume=[];missing=[]
    for f in result["fills"]:
        b=index.get(f["symbol"],{}).get(f["ts_ms"]//DAY*DAY)
        if b is None:missing.append(f)
        elif b.volume<=0 or b.quote_volume<=0:zero_volume.append(f)
    if missing:raise AssertionError("fills_without_market_bar")
    return {"fills_on_zero_volume_bars":len(zero_volume),"zero_volume_fill_details":zero_volume,
            "fill_market_bars_present":True}


def audit_historical_universe(result,histories):
    """Rebuild each top-liquidity selection without HistoricalPanel's caches."""
    from one_trading.walkforward import EXCLUDED,IDENTITY_BOUNDARIES
    indices={s:{b.open_ms:i for i,b in enumerate(bs)} for s,bs in histories.items()}
    c=result["config"]
    excluded=set(c.get("excluded_symbols",[]))
    for p in result.get("plans",[]):
        day=p["signal_day_ms"];eligible=[]
        for symbol,bs in histories.items():
            if symbol=="BTCUSDT" or symbol in excluded or symbol.removesuffix("USDT") in EXCLUDED:continue
            i=indices[symbol].get(day)
            if i is None or i<239:continue
            window=bs[i-239:i+1]
            if window[-1].volume<=0:continue
            if any(b.open_ms-a.open_ms!=DAY for a,b in zip(window,window[1:])):continue
            if any(window[0].open_ms<t<=day for t in IDENTITY_BOUNDARIES.get(symbol,[])):continue
            liquidity=statistics.mean(b.quote_volume for b in window[-30:])
            if liquidity<c["min_quote_volume"]:continue
            eligible.append((symbol,liquidity))
        expected=[s for s,_ in sorted(eligible,key=lambda x:(-x[1],x[0]))[:c["universe_limit"]]]
        if expected!=p["universe"]:raise AssertionError("historical_universe_mismatch:"+date_key(day,"%Y-%m-%d"))
    return {"historical_universe_independently_recomputed":True,"universe_dates_checked":len(result.get("plans",[]))}


def prefix_invariance(result,histories,cutoff):
    """Replay with physically deleted future bars; compare the actual full run."""
    from one_trading.cashflow_alpha import HistoricalPanel, run
    from one_trading.historical_data import ms
    start=ms(result["summary"]["start"])
    end=ms(result["summary"]["end_exclusive"])
    if not start<cutoff<end:raise ValueError("prefix_cutoff_not_inside_run")
    truncated={s:bs2 for s,bs in histories.items() if (bs2:=[b for b in bs if b.open_ms<cutoff])}
    c=result["config"]
    short=run(HistoricalPanel(truncated,c),c,start,cutoff,missing="zero",liquidate_end=False)
    comparisons={"equity":"ts_ms","fills":"ts_ms","writeoffs":"ts_ms","plans":"signal_day_ms"}
    for key,time_key in comparisons.items():
        prefix=[r for r in result[key] if r[time_key]<cutoff]
        if prefix!=short[key]:
            raise AssertionError("future_prefix_changed:"+key)
    return {"future_data_deleted_at":date_key(cutoff,"%Y-%m-%d"),
            "prefix_equity_fills_writeoffs_plans_identical":True,
            "days_checked":len(short["equity"]),"fills_checked":len(short["fills"])}


def block_bootstrap(monthly, repetitions=4000, block_length=3, seed=20261025):
    """Circular moving blocks preserve short-range dependence, not regime shifts."""
    values=list(monthly.values())
    if len(values)<12:
        return {"status":"INSUFFICIENT_MONTHS"}
    rng=random.Random(seed)
    means, medians, growth=[] ,[], []
    for _ in range(repetitions):
        sampled=[]
        while len(sampled)<len(values):
            i=rng.randrange(len(values))
            sampled.extend(values[(i+j)%len(values)] for j in range(block_length))
        sampled=sampled[:len(values)]
        means.append(statistics.mean(sampled));medians.append(statistics.median(sampled))
        growth.append(math.expm1(statistics.mean(math.log1p(v) for v in sampled)*12))
    interval=lambda xs:[quantile(xs,.025),quantile(xs,.975)]
    return {"method":"circular_monthly_moving_block_percentile","block_months":block_length,
            "replications":repetitions,"seed":seed,"n_months":len(values),
            "monthly_mean_95pct_interval":interval(means),
            "monthly_median_95pct_interval":interval(medians),
            "annualized_geometric_return_95pct_interval":interval(growth),
            "fraction_resamples_positive_cagr":sum(g>0 for g in growth)/repetitions,
            "interpretation":"Historical resampling uncertainty only, NOT a calibrated probability of future success. Regime changes, research selection and strategy adaptation are not represented."}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("results",nargs="+",type=Path)
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--archive",type=Path)
    p.add_argument("--prefix-date",help="Requires archive; only for continuous runs spanning this date.")
    args=p.parse_args()
    histories=None
    if args.archive:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from one_trading.historical_data import load_histories,ms
        histories,_=load_histories(args.archive)
    reports={}
    for path in args.results:
        result=json.loads(path.read_text())
        report=recompute(result)
        report["checks"].update(check_signal_chronology(result))
        report["checks"].update(check_reported_summary(result,report))
        if histories is not None:
            report["checks"].update(audit_market_fills(result,histories))
            report["checks"].update(audit_historical_universe(result,histories))
            if args.prefix_date:
                report["checks"].update(prefix_invariance(result,histories,ms(args.prefix_date)))
        report["bootstrap"]=block_bootstrap(report["metrics"]["monthly_returns"])
        reports[str(path)]=report
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(reports,indent=2,allow_nan=False)+"\n")
    print(json.dumps({"checked_files":len(reports),"output":str(args.output)}))


if __name__=="__main__":
    main()
