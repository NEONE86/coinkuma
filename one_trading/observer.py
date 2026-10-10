"""Read-only, unvalidated market observer. No account, order or execution code.

Official source: https://developers.binance.com/en/docs/products/spot/faqs/market_data_only
All signal thresholds below are research hypotheses, never validated trading edge.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DAY = 86_400_000
VERSION = "preposition-heuristic-v0.2-UNVALIDATED"
BASE_URL = "https://data-api.binance.vision"
STABLE_BASES = frozenset({"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PAX", "USD1", "USDE", "USDD", "RLUSD", "PYUSD", "EUR", "EURI", "AEUR", "EURC", "UST", "USTC", "BFUSD"})
# Explicit identifiers, not unsafe substring filters (e.g. JUP is not a leveraged token).
EXCLUDED_BASES = frozenset({"BTCUP", "BTCDOWN", "ETHUP", "ETHDOWN", "BNBUP", "BNBDOWN", "XRPUP", "XRPDOWN", "LINKUP", "LINKDOWN", "ADAUP", "ADADOWN", "DOTUP", "DOTDOWN", "LTCUP", "LTCDOWN", "SUSHIUP", "SUSHIDOWN", "UNIUP", "UNIDOWN", "AAVEUP", "AAVEDOWN", "EOSUP", "EOSDOWN", "TRXUP", "TRXDOWN", "YFIUP", "YFIDOWN"})
ALLOWED_PATHS = frozenset({"/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/klines"})


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DataError(ValueError):
    """Data is not fit for analysis; fail closed."""


class FetchError(RuntimeError):
    pass


class StopRequired(FetchError):
    """Permissions, geography or rate limits: terminate; never rotate hosts."""


@dataclass(frozen=True)
class Record:
    path: str
    params: dict
    fetched_ms: int
    raw: bytes

    @property
    def data(self) -> Any:
        try:
            return json.loads(self.raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DataError("invalid_json") from exc

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StopRequired("HTTP redirect blocked; no host fallback")


class PublicClient:
    """Fixed public market-data host; bounded GETs, no credentials or retries."""
    def __init__(self, timeout: float = 10, min_delay: float = 0.2):
        if not 0 < timeout <= 30 or not 0 <= min_delay <= 10:
            raise ValueError("invalid timeout/delay")
        self.timeout, self.min_delay = timeout, min_delay
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> Record:
        if path not in ALLOWED_PATHS:
            raise StopRequired("Only four public read-only endpoints are permitted")
        params = params or {}
        delay = self.min_delay - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"User-Agent": "ONE-readonly-observer/0.1", "Accept": "application/json"}, method="GET")
        self.last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise DataError("response_too_large")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 407, 418, 429, 451}:
                retry = exc.headers.get("Retry-After", "unspecified") if exc.headers else "unspecified"
                raise StopRequired(f"HTTP {exc.code}; stopped without retry/fallback; Retry-After={retry}") from exc
            raise FetchError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"network_failure: {type(exc).__name__}: {exc}") from exc
        record = Record(path, params, int(time.time() * 1000), raw)
        payload = record.data
        if isinstance(payload, dict) and isinstance(payload.get("code"), (int, float)) and payload["code"] < 0:
            raise FetchError(f"api_error: {payload['code']}")
        return record


@dataclass(frozen=True)
class Bar:
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


def finite_number(value: Any, *, positive: bool = True) -> float:
    if isinstance(value, bool):
        raise DataError("boolean_numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataError("invalid_number") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise DataError("nonfinite_or_invalid_sign")
    return result


def integer_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataError("invalid_timestamp")
    return value


def parse_bars(rows: Any, server_ms: int, minimum: int = 100) -> list[Bar]:
    """Validate UTC daily bars and discard only genuine current open bars."""
    integer_ms(server_ms)
    if not isinstance(rows, list):
        raise DataError("klines_not_list")
    bars: list[Bar] = []
    prior_open = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            raise DataError("invalid_kline_shape")
        start, end = integer_ms(row[0]), integer_ms(row[6])
        if start % DAY or end != start + DAY - 1:
            raise DataError("not_utc_daily_bar")
        if prior_open is not None and start != prior_open + DAY:
            raise DataError("duplicate_out_of_order_or_gap")
        prior_open = start
        o, h, l, c = [finite_number(v) for v in row[1:5]]
        volume, quote = finite_number(row[5], positive=False), finite_number(row[7], positive=False)
        if l > min(o, c) or h < max(o, c) or l > h:
            raise DataError("inconsistent_ohlc")
        if start > server_ms:
            raise DataError("future_bar")
        if end < server_ms:
            bars.append(Bar(start, end, o, h, l, c, volume, quote))
    if len(bars) < minimum:
        raise DataError(f"insufficient_closed_history:{len(bars)}<{minimum}")
    if bars[-1].close_ms != (server_ms // DAY) * DAY - 1:
        raise DataError("stale_latest_closed_bar")
    return bars


def select_universe(exchange: Any, tickers: Any, server_ms: int, limit: int = 40, symbols: list[str] | None = None, excluded: set[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Liquidity-ranked Binance USDT spot only; NOT market-cap top 400."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be 1..200")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("symbols"), list) or not isinstance(tickers, list):
        raise DataError("invalid_universe_payload")
    excluded = set(excluded or ())
    requested = list(dict.fromkeys(symbols or []))
    if any(not re.fullmatch(r"[A-Z0-9]{4,30}", x) for x in requested):
        raise DataError("invalid_watchlist_symbol")
    if len(requested) > limit:
        raise DataError("watchlist_larger_than_limit")
    active = {}
    for item in exchange["symbols"]:
        if not isinstance(item, dict):
            raise DataError("invalid_exchange_symbol")
        symbol, base = item.get("symbol"), item.get("baseAsset")
        if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT" and item.get("isSpotTradingAllowed") is True and base not in STABLE_BASES | EXCLUDED_BASES and base not in excluded and symbol not in excluded:
            active[symbol] = item
    volumes, bad = {}, {}
    seen = set()
    for item in tickers:
        if not isinstance(item, dict):
            raise DataError("invalid_ticker")
        symbol = item.get("symbol")
        if symbol not in active:
            continue
        if symbol in seen:
            raise DataError("duplicate_ticker")
        seen.add(symbol)
        try:
            close = integer_ms(item.get("closeTime"))
            if abs(server_ms - close) > 300_000:
                raise DataError("stale_or_future_ticker")
            volumes[symbol] = finite_number(item.get("quoteVolume"))
        except DataError as exc:
            bad[symbol] = str(exc)
    if requested:
        selected = []
        for symbol in requested:
            if symbol not in active:
                bad[symbol] = "not_eligible_USDT_spot_or_explicitly_excluded"
            elif symbol not in volumes:
                bad.setdefault(symbol, "missing_liquidity_ticker")
            else:
                selected.append(symbol)
    else:
        selected = sorted(volumes, key=lambda symbol: (-volumes[symbol], symbol))[:limit]
    return selected, bad


def complete_weeks(bars: list[Bar]) -> list[Bar]:
    """Calendar UTC Monday-Sunday bars, discarding incomplete boundary weeks."""
    groups: dict[int, list[Bar]] = {}
    for bar in bars:
        day = bar.open_ms // DAY
        monday = day - ((day + 3) % 7)  # Unix epoch was a Thursday.
        groups.setdefault(monday, []).append(bar)
    result = []
    for monday, group in sorted(groups.items()):
        if len(group) == 7 and group[0].open_ms == monday * DAY and group[-1].open_ms == (monday + 6) * DAY:
            result.append(Bar(group[0].open_ms, group[-1].close_ms, group[0].open, max(x.high for x in group), min(x.low for x in group), group[-1].close, sum(x.volume for x in group), sum(x.quote_volume for x in group)))
    return result


def complete_months(bars: list[Bar]) -> list[Bar]:
    groups = {}
    for bar in bars:
        date = datetime.fromtimestamp(bar.open_ms / 1000, timezone.utc)
        groups.setdefault((date.year, date.month), []).append(bar)
    result = []
    for (year, month), group in sorted(groups.items()):
        first = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        days = calendar.monthrange(year, month)[1]
        if len(group) == days and all(b.open_ms == first + i * DAY for i, b in enumerate(group)):
            result.append(Bar(first, group[-1].close_ms, group[0].open,
                              max(b.high for b in group), min(b.low for b in group),
                              group[-1].close, sum(b.volume for b in group),
                              sum(b.quote_volume for b in group)))
    return result


def analyze(symbol: str, bars: list[Bar], btc: list[Bar] | None) -> dict:
    result = {"symbol": symbol, "state": "SKIP", "strategy": VERSION, "validation": "UNVALIDATED", "reasons": [], "metrics": {}, "bar_close_ms": bars[-1].close_ms if bars else None}
    if not btc or not bars:
        result["reasons"] = ["BTC_CONTEXT_MISSING" if not btc else "ASSET_DATA_MISSING"]
        return result
    if len(bars) < 100 or len(btc) < 100:
        result["reasons"] = ["INSUFFICIENT_HISTORY"]
        return result
    if [x.open_ms for x in bars[-100:]] != [x.open_ms for x in btc[-100:]]:
        result["reasons"] = ["BTC_DATE_MISALIGNMENT"]
        return result
    closes, btc_closes = [x.close for x in bars], [x.close for x in btc]
    last = closes[-1]
    ret7, ret30 = last / closes[-8] - 1, last / closes[-31] - 1
    rs7 = (last / closes[-8]) / (btc_closes[-1] / btc_closes[-8]) - 1
    rs_prior7 = (closes[-8] / closes[-15]) / (btc_closes[-8] / btc_closes[-15]) - 1
    rs20 = (last / closes[-21]) / (btc_closes[-1] / btc_closes[-21]) - 1
    width20 = max(x.high for x in bars[-20:]) / min(x.low for x in bars[-20:]) - 1
    range_recent = statistics.mean((x.high - x.low) / x.close for x in bars[-14:])
    range_prior = statistics.mean((x.high - x.low) / x.close for x in bars[-28:-14])
    contraction = range_recent / range_prior if range_prior > 0 else None
    holding_low = min(x.low for x in bars[-10:]) >= min(x.low for x in bars[-20:-10]) * 0.99
    extension = last / statistics.mean(closes[-20:]) - 1
    btc_ret7 = btc_closes[-1] / btc_closes[-8] - 1
    btc_regime = "RISK_OFF" if btc_closes[-1] < statistics.mean(btc_closes[-50:]) * 0.95 or btc_ret7 < -0.08 else "NEUTRAL_OR_SUPPORTIVE"
    weeks = complete_weeks(bars)
    months = complete_months(bars)
    weekly_ret4 = weeks[-1].close / weeks[-5].close - 1 if len(weeks) >= 5 else None
    result["metrics"] = {"return_7d": ret7, "return_30d": ret30, "relative_BTC_7d": rs7, "relative_BTC_prior_7d": rs_prior7, "relative_BTC_20d": rs20, "base_width_20d": width20, "range_contraction_14d": contraction, "holding_low": holding_low, "extension_above_SMA20": extension, "BTC_regime": btc_regime, "complete_week_count": len(weeks), "complete_week_return_4w": weekly_ret4, "last_close": last}
    result["metrics"].update({"complete_month_count": len(months),
                              "complete_month_return_3m": months[-1].close / months[-4].close - 1 if len(months) >= 4 else None})
    result["missing_evidence"] = ["historical_unlocks", "real_usage_and_revenue", "token_value_capture", "macro_liquidity", "executable_reward_risk"]
    if symbol == "BTCUSDT":
        result["state"], result["reasons"] = "CONTEXT", ["BTC_BENCHMARK_ONLY"]
        return result
    if btc_regime == "RISK_OFF":
        result["reasons"] = ["BTC_RISK_OFF"]
        return result
    if ret7 > 0.12 or ret30 > 0.35 or extension > 0.10:
        result["reasons"] = ["NO_SURGE_CHASING"]
        return result
    checks = {"BASE_FORMING": width20 <= 0.20 and holding_low, "VOLATILITY_CONTRACTING": contraction is not None and contraction <= 0.85, "BTC_RELATIVE_IMPROVING": rs20 > 0 and rs7 > rs_prior7, "WEEKLY_NOT_BREAKING": weekly_ret4 is not None and weekly_ret4 > -0.08}
    result["score"] = 25 * sum(checks.values())
    result["state"] = "PREPOSITION_CANDIDATE" if all(checks.values()) else "WATCH"
    result["reasons"] = [key if passed else "NOT_" + key for key, passed in checks.items()]
    result["meaning"] = "검증 전 선행 관찰 후보. 매수 지시·수익 보장·자금 이동 증거가 아님."
    return result


class Store:
    """Immutable raw snapshots, append-only decisions/health/outcomes, atomic scans."""
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY, path TEXT NOT NULL, params TEXT NOT NULL,
          fetched_ms INTEGER NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL,
          UNIQUE(path, params, sha256));
        CREATE TABLE IF NOT EXISTS decisions (
          id TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL, symbol TEXT NOT NULL,
          bar_close_ms INTEGER, state TEXT NOT NULL, strategy TEXT NOT NULL,
          snapshot_id INTEGER REFERENCES snapshots(id), payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS decision_symbol_time ON decisions(symbol,observed_ms);
        CREATE TABLE IF NOT EXISTS outcomes (
          decision_id TEXT NOT NULL REFERENCES decisions(id), horizon_days INTEGER NOT NULL,
          evaluated_ms INTEGER NOT NULL, entry_open_ms INTEGER NOT NULL,
          exit_close_ms INTEGER NOT NULL, gross_markout REAL NOT NULL,
          btc_gross_markout REAL, excess_vs_btc REAL, payload TEXT NOT NULL,
          PRIMARY KEY(decision_id,horizon_days));
        CREATE TABLE IF NOT EXISTS health (
          id INTEGER PRIMARY KEY, observed_ms INTEGER NOT NULL, status TEXT NOT NULL,
          detail TEXT NOT NULL);
        """)
        for table in ("snapshots", "decisions", "outcomes", "health"):
            for action in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS no_{action.lower()}_{table} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END")
        self.db.commit()

    def close(self):
        self.db.close()

    def snapshot(self, record: Record) -> int:
        params = json_text(record.params)
        self.db.execute("INSERT OR IGNORE INTO snapshots(path,params,fetched_ms,sha256,raw) VALUES(?,?,?,?,?)", (record.path, params, record.fetched_ms, record.sha256, record.raw))
        return self.db.execute("SELECT id FROM snapshots WHERE path=? AND params=? AND sha256=?", (record.path, params, record.sha256)).fetchone()[0]

    def decision(self, result: dict, observed_ms: int, snapshot_id: int | None = None) -> bool:
        # A change of classification/reasons creates a new event. Repeated scans/restarts
        # of the same closed bar and classification do not reset its first observation.
        identity = [VERSION, result["symbol"], result.get("bar_close_ms") or observed_ms // DAY * DAY, result["state"], result["reasons"]]
        key = hashlib.sha256(json_text(identity).encode()).hexdigest()
        cursor = self.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?,?,?)", (key, observed_ms, result["symbol"], result.get("bar_close_ms"), result["state"], VERSION, snapshot_id, json_text(result)))
        return bool(cursor.rowcount)

    def health(self, status: str, detail: dict, observed_ms: int | None = None):
        self.db.execute("INSERT INTO health(observed_ms,status,detail) VALUES(?,?,?)", (observed_ms or int(time.time() * 1000), status, json_text(detail)))

    def pending_symbols(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT symbol FROM decisions d WHERE bar_close_ms IS NOT NULL AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.decision_id=d.id AND o.horizon_days=84)")
        return [row[0] for row in rows]

    def record_outcomes(self, all_bars: dict[str, list[Bar]], evaluated_ms: int) -> int:
        """Forward observation markouts, not fills/PnL; includes WATCH and SKIP.

        Entry reference is the first UTC daily open strictly AFTER the actual
        decision observation, never the already-known signal close. Horizon is
        1/3/7/28/84 completed daily bars from that reference. No partial candles.
        """
        count = 0
        btc_by_open = {b.open_ms: b for b in all_bars.get("BTCUSDT", [])}
        for decision in self.db.execute("SELECT * FROM decisions WHERE bar_close_ms IS NOT NULL").fetchall():
            sequence = all_bars.get(decision["symbol"], [])
            target_open = (decision["observed_ms"] // DAY + 1) * DAY
            future = [b for b in sequence if b.open_ms >= target_open and b.close_ms < evaluated_ms]
            # Exact next open required. Never silently move the baseline after outages.
            if not future or future[0].open_ms != target_open:
                continue
            for horizon in (1, 3, 7, 28, 84):
                if len(future) < horizon:
                    continue
                entry, exit_bar = future[0], future[horizon - 1]
                if exit_bar.open_ms != entry.open_ms + (horizon - 1) * DAY:
                    continue
                gross = exit_bar.close / entry.open - 1
                btc_entry, btc_exit = btc_by_open.get(entry.open_ms), btc_by_open.get(exit_bar.open_ms)
                btc_return = btc_exit.close / btc_entry.open - 1 if btc_entry and btc_exit else None
                detail = {"type": "FORWARD_MARKOUT_NOT_TRADING_PNL", "decision_state": decision["state"], "entry_reference": "first_daily_open_strictly_after_observation", "fees_slippage_funding": "NOT_INCLUDED_NO_EXECUTION", "realized_profit": False}
                cursor = self.db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?,?,?,?)", (decision["id"], horizon, evaluated_ms, entry.open_ms, exit_bar.close_ms, gross, btc_return, gross - btc_return if btc_return is not None else None, json_text(detail)))
                count += cursor.rowcount
        return count

    def status(self) -> dict:
        latest = self.db.execute("SELECT observed_ms,status,detail FROM health ORDER BY id DESC LIMIT 1").fetchone()
        counts = {table: self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("snapshots", "decisions", "outcomes")}
        return {"last_health": dict(latest) if latest else None, "counts": counts, "mode": "READ_ONLY", "validation": "UNVALIDATED", "live_trading": False}


def scan_once(client: PublicClient, store: Store, *, limit: int = 40, symbols: list[str] | None = None, excluded: set[str] | None = None, history_days: int = 240) -> dict:
    if not 100 <= history_days <= 999:
        raise ValueError("history_days must be 100..999")
    bars_by_symbol, snapshots, errors, results = {}, {}, {}, []
    server_ms = None
    try:
        with store.db:
            clock_record = client.get("/api/v3/time")
            store.snapshot(clock_record)
            server_ms = integer_ms(clock_record.data.get("serverTime"))
            if abs(server_ms - clock_record.fetched_ms) > 300_000:
                raise DataError("server_clock_skew_exceeds_5min")
            exchange = client.get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
            tickers = client.get("/api/v3/ticker/24hr")
            store.snapshot(exchange)
            store.snapshot(tickers)
            selected, rejected = select_universe(exchange.data, tickers.data, server_ms, limit, symbols, excluded)
            errors.update(rejected)
            # Continue measuring old decisions even when symbols leave today's pool.
            # Bound work to 200 tracked symbols and report any overflow explicitly.
            tracked = store.pending_symbols()
            priority = list(dict.fromkeys(["BTCUSDT"] + selected + tracked))
            if len(priority) > 200:
                errors["TRACKING_CAP"] = f"{len(priority)-200} symbols deferred; outcomes may be missing"
                priority = priority[:200]
            for symbol in priority:
                try:
                    record = client.get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": history_days + 1, "endTime": server_ms})
                    snapshots[symbol] = store.snapshot(record)
                    bars_by_symbol[symbol] = parse_bars(record.data, server_ms)
                except (FetchError, DataError) as exc:
                    if isinstance(exc, StopRequired):
                        raise
                    errors[symbol] = str(exc)
            # Use completion time, not scan start, as decision time (no look-ahead
            # from candles or ticker responses observed later during this scan).
            observed_ms = max(server_ms, int(time.time() * 1000))
            btc = bars_by_symbol.get("BTCUSDT")
            for symbol in list(dict.fromkeys(["BTCUSDT"] + selected + list(rejected))):
                if symbol in bars_by_symbol and symbol not in rejected:
                    result = analyze(symbol, bars_by_symbol[symbol], btc)
                else:
                    result = {"symbol": symbol, "state": "SKIP", "strategy": VERSION, "validation": "UNVALIDATED", "reasons": [errors.get(symbol, "ASSET_DATA_MISSING")], "metrics": {}, "bar_close_ms": None}
                result["new_event"] = store.decision(result, observed_ms, snapshots.get(symbol))
                results.append(result)
            outcome_count = store.record_outcomes(bars_by_symbol, observed_ms)
            status = "OK" if btc and not errors else "DEGRADED"
            summary = {"status": status, "mode": "READ_ONLY", "validation": "UNVALIDATED", "server_ms": server_ms, "observed_ms": observed_ms, "universe": "Binance USDT spot, 24h quote-volume ranked; NOT market-cap top400", "selected_count": len(selected), "decision_count": len(results), "new_events": sum(x["new_event"] for x in results), "new_outcomes": outcome_count, "errors": errors, "decisions": sorted(results, key=lambda x: (-x.get("score", -1), x["symbol"])), "notice": "주문·자산운용 없음. 데이터 기반 관찰 후보이며 전략 우위는 미검증."}
            store.health(status, {key: value for key, value in summary.items() if key != "decisions"}, observed_ms)
            return summary
    except (FetchError, DataError, TypeError, AttributeError, KeyError) as exc:
        # Atomic transaction rolled back: no partial opportunities escape a scan
        # halted by authorization/rate limits or a corrupted universe.
        status = "STOP_REQUIRED" if isinstance(exc, StopRequired) else "ERROR"
        summary = {"status": status, "mode": "READ_ONLY", "validation": "UNVALIDATED", "error": str(exc), "decisions": [], "new_events": 0, "new_outcomes": 0}
        with store.db:
            store.health(status, summary)
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ONE 읽기 전용 시장 관찰기 — 거래/수익 보장 없음")
    parser.add_argument("command", choices=["scan", "run", "status"])
    parser.add_argument("--db", default="data/observer.sqlite3")
    parser.add_argument("--symbols", help="Comma-separated explicit Binance USDT spot watchlist")
    parser.add_argument("--exclude", default="", help="Exact symbol or base IDs, comma-separated")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--history-days", type=int, default=240)
    parser.add_argument("--interval", type=int, default=900, help="Polling seconds, minimum 300")
    parser.add_argument("--cycles", type=int, default=0, help="run cycles; 0 continues until Ctrl+C")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 200 or not 100 <= args.history_days <= 999 or args.interval < 300 or args.cycles < 0:
        parser.error("limit 1..200, history-days 100..999, interval>=300, cycles>=0 required")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] if args.symbols else None
    excluded = {x.strip().upper() for x in args.exclude.split(",") if x.strip()}
    if symbols and (len(symbols) > args.limit or any(not re.fullmatch(r"[A-Z0-9]{4,30}", symbol) for symbol in symbols)):
        parser.error("invalid watchlist or watchlist exceeds --limit")
    store = Store(args.db)
    try:
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return 0
        client, cycles = PublicClient(), 0
        while True:
            summary = scan_once(client, store, limit=args.limit, symbols=symbols, excluded=excluded, history_days=args.history_days)
            print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
            cycles += 1
            if summary["status"] == "STOP_REQUIRED":
                return 3
            if args.command == "scan" or (args.cycles and cycles >= args.cycles):
                return 0 if summary["status"] == "OK" else 2
            # Interruptible bounded wait chunks: process is externally supervised,
            # not a promise that this chat or an ephemeral workspace runs forever.
            remaining = args.interval
            while remaining:
                step = min(remaining, 30)
                threading.Event().wait(step)
                remaining -= step
    except KeyboardInterrupt:
        with store.db:
            store.health("STOPPED", {"reason": "operator_interrupt"})
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
