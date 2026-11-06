"""Strict, point-in-time OHLCV panel for research; no exchange calls."""
from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median


class DataError(ValueError):
    pass


BASE_FEATURES = (
    "r1", "r4", "r16", "range", "body", "volume_change", "volume_relative",
    "relative_r1", "relative_r4",
)
OPTIONAL_FEATURES = ("oi_change", "flow_imbalance", "depth_imbalance", "funding", "spread")
STATE_FEATURES = ("position", "position_age", "unrealized", "drawdown")


def bounded(x: float, cap: float = 3.0) -> float:
    return min(cap, max(-cap, float(x)))


def timestamp(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise DataError("INVALID_TIMESTAMP") from e
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise DataError("TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    extras: dict[str, float]


@dataclass
class Market:
    symbols: tuple[str, ...]
    times: tuple[datetime, ...]
    series: dict[str, tuple[Bar, ...]]
    features: dict[str, list[dict[str, float]]]
    active_features: tuple[str, ...]
    source_hash: str
    funding_known: bool
    source_type: str = "UNVERIFIED_EXTERNAL_CSV"


def load_csv(path: str | Path, *, source_type: str = "UNVERIFIED_EXTERNAL_CSV") -> Market:
    content = Path(path).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    try:
        raw = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DataError("NOT_UTF8") from e
    import io
    reader = csv.DictReader(io.StringIO(raw))
    required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise DataError("REQUIRED_COLUMNS_MISSING")
    optional = {"oi", "aggressive_buy", "aggressive_sell", "bid_depth", "ask_depth", "spread_bps", "funding_rate"}
    optional_used = set(reader.fieldnames) & optional
    grouped: dict[str, list[Bar]] = {}
    for count, row in enumerate(reader, 1):
        if count > 2_000_000:
            raise DataError("INPUT_ROW_CAP")
        symbol = (row["symbol"] or "").strip().upper()
        if not symbol.isalnum() or not 2 <= len(symbol) <= 25:
            raise DataError(f"INVALID_SYMBOL: row={count}")
        try:
            vals = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
            extras = {k: float(row[k]) for k in optional_used}
        except (TypeError, ValueError, KeyError) as e:
            raise DataError(f"INVALID_NUMBER: row={count}") from e
        if any(not math.isfinite(v) for v in vals) or any(not math.isfinite(v) for v in extras.values()):
            raise DataError(f"NONFINITE: row={count}")
        o, h, l, c, v = vals
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c) or l > min(o, c) or l > h:
            raise DataError(f"INVALID_OHLC: row={count}")
        for k in ("oi", "aggressive_buy", "aggressive_sell", "bid_depth", "ask_depth", "spread_bps"):
            if k in extras and extras[k] < 0:
                raise DataError(f"NEGATIVE_OPTIONAL: row={count}")
        grouped.setdefault(symbol, []).append(Bar(timestamp(row["timestamp"]), o, h, l, c, v, extras))
    if not 1 <= len(grouped) <= 12:
        raise DataError("UNIVERSE_SIZE_OUT_OF_RANGE")
    names = tuple(sorted(grouped))
    times = tuple(b.t for b in grouped[names[0]])
    if len(times) < 200:
        raise DataError("MINIMUM_200_BARS")
    if len(set(times)) != len(times) or list(times) != sorted(times):
        raise DataError("TIMESTAMP_NOT_STRICTLY_INCREASING")
    dt = times[1] - times[0]
    if dt <= timedelta(0) or any(times[i] - times[i-1] != dt for i in range(1, len(times))):
        raise DataError("MISSING_OR_IRREGULAR_CANDLES")
    for symbol, bars in grouped.items():
        if tuple(b.t for b in bars) != times:
            raise DataError(f"SYMBOL_TIMESTAMP_MISMATCH:{symbol}")
    active = list(BASE_FEATURES)
    if "oi" in optional_used: active.append("oi_change")
    if "aggressive_buy" in optional_used and "aggressive_sell" in optional_used:
        active.append("flow_imbalance")
    if "bid_depth" in optional_used and "ask_depth" in optional_used:
        active.append("depth_imbalance")
    if "funding_rate" in optional_used: active.append("funding")
    if "spread_bps" in optional_used: active.append("spread")

    feats: dict[str, list[dict[str, float]]] = {}
    for symbol in names:
        arr = grouped[symbol]
        result = []
        for i, b in enumerate(arr):
            p1 = arr[max(0, i-1)]
            p4 = arr[max(0, i-4)]
            p16 = arr[max(0, i-16)]
            mvol = sum(x.v for x in arr[max(0,i-15):i+1]) / min(16,i+1)
            f = {
                "r1": bounded(20*math.log(b.c/p1.c)),
                "r4": bounded(10*math.log(b.c/p4.c)),
                "r16": bounded(5*math.log(b.c/p16.c)),
                "range": bounded(10*(b.h-b.l)/b.c),
                "body": bounded(20*(b.c-b.o)/b.c),
                "volume_change": bounded(math.log((b.v+1)/(p1.v+1))),
                "volume_relative": bounded(math.log((b.v+1)/(mvol+1))),
            }
            if "oi_change" in active:
                f["oi_change"] = bounded(10*math.log((b.extras["oi"]+1)/(p1.extras["oi"]+1)))
            if "flow_imbalance" in active:
                buy,sell=b.extras["aggressive_buy"],b.extras["aggressive_sell"]
                f["flow_imbalance"] = (buy-sell)/(buy+sell+1e-12)
            if "depth_imbalance" in active:
                bid,ask=b.extras["bid_depth"],b.extras["ask_depth"]
                f["depth_imbalance"] = (bid-ask)/(bid+ask+1e-12)
            if "funding" in active: f["funding"] = bounded(1000*b.extras["funding_rate"])
            if "spread" in active: f["spread"] = bounded(b.extras["spread_bps"]/50)
            result.append(f)
        feats[symbol] = result
    for i in range(len(times)):
        med1 = median(feats[s][i]["r1"] for s in names)
        med4 = median(feats[s][i]["r4"] for s in names)
        for s in names:
            feats[s][i]["relative_r1"] = bounded(feats[s][i]["r1"]-med1)
            feats[s][i]["relative_r4"] = bounded(feats[s][i]["r4"]-med4)
    return Market(names, times, {s:tuple(grouped[s]) for s in names}, feats,
                  tuple(active+list(STATE_FEATURES)), digest,
                  "funding_rate" in optional_used, source_type)
