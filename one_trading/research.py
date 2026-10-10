"""Reproducible exploratory study, not a trading backtest or alpha certificate.

No fitted parameters. Today's surviving liquid universe is explicitly biased.
Historical data cannot test the fundamental value-capture hypothesis by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .observer import DAY, NoRedirect, PublicClient, Store, analyze, parse_bars, scan_once
from .governance import GovernanceStore

SPEC = {
    "id": "ONE-price-proxy-comparison-v1",
    "question": "Do base/contraction/relative-strength proxies outperform low-price and momentum proxies?",
    "rules": {
        "early_base": "observer PREPOSITION_CANDIDATE, unchanged thresholds",
        "low_price": "close <= 70% of trailing 100-day high",
        "momentum": "30-day return > 20%",
        "equal_weight_pool": "every available non-BTC asset in sampled pool",
    },
    "lookback_days": 240,
    "holding_days": 28,
    "entry": "open two UTC days after signal day starts, allowing one full day to collect data",
    "cohort_stride_days": 29,
    "roundtrip_cost_fraction": 0.002,
    "stress_roundtrip_cost_fraction": 0.006,
    "cost_model": "gross return minus assumed cost as fraction of entry notional; spot, no funding",
    "scope": "EXPLORATORY_SURVIVOR_BIASED_NOT_OOS_NOT_EXECUTABLE_PNL",
    "promotion": "BLOCKED: no point-in-time universe, no fundamental panel, no forward evidence",
}


def save_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def cap_snapshot(output):
    """Two documented public pages; retain raw data and observed coverage.

    No retries, proxy, key discovery or alternate host after denial/rate limit.
    IDs, not tickers, identify coins. No automated cross-exchange ticker join.
    """
    opener = urllib.request.build_opener(NoRedirect())
    coins, pages, errors = {}, [], []
    for page in (1, 2):
        params = dict(vs_currency="usd", order="market_cap_desc", per_page=250,
                      page=page, sparkline="false", price_change_percentage="7d,30d")
        url = "https://api.coingecko.com/api/v3/coins/markets?" + urllib.parse.urlencode(params)
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "ONE-research/0.2"}), timeout=30) as r:
                raw = r.read(5_000_001)
            if len(raw) > 5_000_000:
                raise ValueError("oversized response")
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError("not a market list")
            observed = datetime.now(timezone.utc).isoformat()
            rawpath = Path(output) / f"coingecko_page_{page}.json"
            rawpath.parent.mkdir(parents=True, exist_ok=True)
            rawpath.write_bytes(raw)
            pages.append(dict(url=url, observed_at=observed, sha256=hashlib.sha256(raw).hexdigest()))
            for row in rows:
                rank = row.get("market_cap_rank")
                if type(rank) is int and 1 <= rank <= 400:
                    coins[row["id"]] = row
        except Exception as exc:
            errors.append(f"page {page}: {type(exc).__name__}: {exc}")
            break
        time.sleep(0.3)
    result = {"sources": pages, "errors": errors, "unique_ids": len(coins),
              "unique_ranks": len({x["market_cap_rank"] for x in coins.values()}),
              "coverage": "current cap ranks 1..400 snapshot only; not 400 historical/fundamental analyses",
              "breadth_caveat": "stablecoins/wrapped assets included; not cleaned altcoin breadth",
              "coins": sorted(coins.values(), key=lambda x: x["market_cap_rank"])}
    save_json(Path(output) / "cap400_snapshot.json", result)
    return result


def load_history(dbpath):
    connection = sqlite3.connect(dbpath)
    try:
        clock = connection.execute("SELECT raw FROM snapshots WHERE path='/api/v3/time' ORDER BY id DESC LIMIT 1").fetchone()
        if not clock:
            raise ValueError("No successful public snapshot in database")
        server = json.loads(clock[0])["serverTime"]
        histories, hashes = {}, {}
        for params, raw, digest in connection.execute("SELECT params,raw,sha256 FROM snapshots WHERE path='/api/v3/klines' ORDER BY id"):
            p = json.loads(params)
            if p.get("interval") == "1d":
                try:
                    histories[p["symbol"]] = parse_bars(json.loads(raw), server)
                    hashes[p["symbol"]] = digest
                except ValueError:
                    pass
        return histories, hashes
    finally:
        connection.close()


def study(histories, cost=0.002):
    if not 0 <= cost < 0.1:
        raise ValueError("invalid cost")
    btc = histories.get("BTCUSDT", [])
    if len(btc) < 270:
        return {"status": "INSUFFICIENT_HISTORY", "cohorts": [], "summary": {}}
    indexed = {s: {b.open_ms: i for i, b in enumerate(bars)} for s, bars in histories.items()}
    cohorts = []
    horizon = SPEC["holding_days"]
    # Whole-portfolio dates never overlap. Assets on one date are correlated,
    # so report dates, not asset-count, as the sampling unit.
    for i in range(SPEC["lookback_days"] - 1, len(btc) - horizon - 1, SPEC["cohort_stride_days"]):
        signal = btc[i].open_ms
        entrytime, exittime = signal + 2 * DAY, signal + (horizon + 1) * DAY
        groups = {k: [] for k in SPEC["rules"]}
        details = []
        btc_return = btc[i + horizon + 1].close / btc[i + 2].open - 1 - cost
        for symbol, bars in histories.items():
            if symbol == "BTCUSDT":
                continue
            idx = indexed[symbol]
            j, e, x = idx.get(signal), idx.get(entrytime), idx.get(exittime)
            if j is None or j < 99 or e is None or x is None or x - e != horizon - 1:
                continue
            window = bars[max(0, j - 239):j + 1]
            decision = analyze(symbol, window, btc[max(0, i - 239):i + 1])
            flags = {
                "early_base": decision["state"] == "PREPOSITION_CANDIDATE",
                "low_price": bars[j].close <= max(b.high for b in window[-100:]) * 0.7,
                "momentum": bars[j].close / bars[j - 30].close - 1 > 0.2,
                "equal_weight_pool": True,
            }
            gross = bars[x].close / bars[e].open - 1
            for rule, selected in flags.items():
                if selected:
                    groups[rule].append(gross - cost)
            details.append({"symbol": symbol, "selected": flags, "gross_28d": gross,
                            "net_28d": gross - cost, "entry_open": bars[e].open,
                            "exit_close": bars[x].close})
        cohorts.append({"signal_ms": signal, "entry_ms": entrytime, "exit_ms": exittime + DAY - 1,
                        "BTC_net": btc_return, "cash": 0.0, "assets": details,
                        "rules": {k: {"n": len(v), "net": statistics.mean(v) if v else 0.0,
                                       "active": bool(v)} for k, v in groups.items()}})
    summary = {}
    for rule in SPEC["rules"]:
        active = [c for c in cohorts if c["rules"][rule]["active"]]
        returns = [c["rules"][rule]["net"] for c in cohorts]
        summary[rule] = {
            "total_dates": len(cohorts), "active_dates": len(active),
            "cash_dates": len(cohorts) - len(active),
            "mean_net_per_28d_cohort_cash_when_inactive": statistics.mean(returns) if returns else None,
            "mean_active_net": statistics.mean(c["rules"][rule]["net"] for c in active) if active else None,
            "mean_active_excess_vs_BTC": statistics.mean(c["rules"][rule]["net"] - c["BTC_net"] for c in active) if active else None,
            "worst_cohort_net": min(returns) if returns else None,
        }
    return {"status": "EXPLORATORY_ONLY", "cost": cost, "cohorts": cohorts, "summary": summary,
            "BTC_mean_same_all_dates": statistics.mean(c["BTC_net"] for c in cohorts) if cohorts else None,
            "warnings": ["Current survivors and liquidity selection bias historical outcomes",
                         "No historical cap400 membership; no delisted assets",
                         "No parameter fitting, but historical replay is NOT out-of-sample validation",
                         "No marked-equity drawdown, actual fills, stop/target, or validated 3R",
                         "No macro, token value capture, revenue or unlock series",
                         "Returns are cohort markouts, NOT realized profits or compounding results"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/observer.sqlite3")
    parser.add_argument("--output", default="research_runs")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--cap400", action="store_true")
    args = parser.parse_args()
    output = Path(args.output) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    # Freeze a spec plus exact source hashes before data refresh or replay.
    manifest = {"registered_at": datetime.now(timezone.utc).isoformat(), "spec": SPEC,
                "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}}
    save_json(output / "manifest.json", manifest)
    if args.cap400:
        cap_snapshot(output)
    if args.refresh:
        store = Store(args.db)
        try:
            scan = scan_once(PublicClient(), store, limit=40, history_days=999)
            save_json(output / "scan.json", scan)
            if scan["status"] in {"ERROR", "STOP_REQUIRED"}:
                print(json.dumps({"output": str(output), "status": scan["status"]}))
                return 2
        finally:
            store.close()
    try:
        histories, hashes = load_history(args.db)
        report = {"manifest": manifest, "dataset_hashes": hashes,
                  "coverage": {s: len(b) for s, b in histories.items()},
                  "base": study(histories, SPEC["roundtrip_cost_fraction"]),
                  "stress": study(histories, SPEC["stress_roundtrip_cost_fraction"])}
        save_json(output / "study.json", report)
        report_hash = hashlib.sha256((output / "study.json").read_bytes()).hexdigest()
        strategy_hash = hashlib.sha256(json.dumps({"spec": SPEC, "code": manifest["code_sha256"]}, sort_keys=True).encode()).hexdigest()
        with GovernanceStore(Path(args.db).parent / "governance.sqlite3") as ledger:
            if not ledger.connection.execute("SELECT 1 FROM strategies WHERE strategy_id=?", (strategy_hash,)).fetchone():
                ledger.register_strategy(strategy_hash, {"spec": SPEC, "code": manifest["code_sha256"]},
                                         experiment_id=SPEC["id"], description="Exploratory price proxy, fundamentals absent")
            ledger.record_attempt(strategy_hash, "INCONCLUSIVE", {
                "report": str(output / "study.json"), "report_sha256": report_hash,
                "reasons": ["survivor_biased", "not_out_of_sample", "no_fundamental_evidence", "no_forward_performance"],
                "summaries": report["base"]["summary"]})
        print(json.dumps({"output": str(output), "coverage": report["coverage"],
                          "summary": report["base"]["summary"], "status": report["base"]["status"]}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        save_json(output / "error.json", {"error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps({"output": str(output), "status": "ERROR", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
