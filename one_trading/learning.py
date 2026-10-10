"""Small trainable ridge model; learns only from already-matured 28-day labels.

No LLM, API key, numpy or fitted-in-advance standardizer. The features are scaled
by fixed constants. Target is a clipped log markout, not a calibrated probability
or the eventual realized return of a stopped/rotating portfolio.
"""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import math
import statistics
from typing import Any
from .observer import DAY, Bar, analyze, complete_weeks, complete_months, json_text

FEATURE_NAMES = ["intercept", "return_7d", "return_30d", "btc_relative_7d",
                 "btc_relative_20d", "base_width", "contraction", "holding_low",
                 "extension_sma20", "weekly_return_4w", "monthly_return_3m",
                 "below_180d_high", "quote_volume_acceleration", "btc_return_28d"]
D = len(FEATURE_NAMES)

def clip(v: float, lo: float = -3., hi: float = 3.) -> float:
    return max(lo, min(hi, float(v)))

def feature(symbol: str, bars: list[Bar], btc: list[Bar]) -> dict | None:
    if len(bars) < 180 or len(btc) < 180:
        return None
    window, bc = bars[-240:], btc[-240:]
    r = analyze(symbol, window, bc)
    m = r.get("metrics", {})
    if not m or m.get("complete_month_return_3m") is None:
        return None
    if r["state"] == "CONTEXT":
        return None
    closes = [b.close for b in window]
    vol_recent = statistics.mean(b.quote_volume for b in window[-7:])
    vol_old = statistics.mean(b.quote_volume for b in window[-35:-7])
    accel = vol_recent / vol_old if vol_old > 0 else 1.
    dd = 1 - closes[-1] / max(b.high for b in window[-180:])
    x = [1., m["return_7d"]/.10, m["return_30d"]/.30,
         m["relative_BTC_7d"]/.10, m["relative_BTC_20d"]/.20,
         (m["base_width_20d"]-.25)/.25,
         ((m["range_contraction_14d"] or 1.)-1.)/.5,
         float(m["holding_low"]), m["extension_above_SMA20"]/.10,
         (m["complete_week_return_4w"] or 0.)/.30,
         m["complete_month_return_3m"]/.60,
         dd/.50, (accel-1.)/2., (bc[-1].close/bc[-29].close-1.)/.20]
    r["x"] = [clip(v) for v in x]
    r["metrics"]["below_180d_high"] = dd
    r["metrics"]["quote_volume_acceleration"] = accel
    # A support estimate, not an executed stop or a promise of bounded loss.
    trs = [max(b.high-b.low, abs(b.high-window[i-1].close),
               abs(b.low-window[i-1].close)) for i,b in enumerate(window) if i>0][-14:]
    atr = statistics.mean(trs)
    r["support_stop"] = min(b.low for b in window[-20:]) - .25*atr
    r["signal_ms"] = window[-1].close_ms
    return r


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Partial-pivot elimination on a tiny SPD ridge system."""
    n = len(b)
    m = [list(row)+[v] for row,v in zip(a,b)]
    for i in range(n):
        pivot = max(range(i,n), key=lambda j: abs(m[j][i]))
        if abs(m[pivot][i]) < 1e-12:
            raise ValueError("singular_learning_system")
        m[i],m[pivot] = m[pivot],m[i]
        q = m[i][i]
        m[i] = [v/q for v in m[i]]
        for j in range(n):
            if j != i:
                q = m[j][i]
                if q:
                    m[j] = [v-q*w for v,w in zip(m[j],m[i])]
    result = [row[-1] for row in m]
    if not all(math.isfinite(v) for v in result):
        raise ValueError("nonfinite_learning_weights")
    return result


class MaturedRidge:
    def __init__(self, config: dict):
        self.config = config
        self.xtx = [[0.]*D for _ in range(D)]
        self.xty = [[0.]*D for _ in range(2)]
        self.groups = deque()
        self.weights = None
        self.fit_ms = None
        self.max_label_close_ms = None
        self.fit_dates = self.fit_samples = 0

    def _apply(self, items: list[dict], sign: float) -> None:
        weight = sign/len(items)  # each date has total weight one
        for row in items:
            x,ys = row["x"],row["ys"]
            for i,xi in enumerate(x):
                wx = weight*xi
                for j,xj in enumerate(x):
                    self.xtx[i][j] += wx*xj
                for k in (0,1):
                    self.xty[k][i] += wx*ys[k]

    def learn(self, label_close_ms: int, items: list[dict], asof_ms: int) -> None:
        if label_close_ms > asof_ms:
            raise ValueError("future_label_rejected")
        if self.groups and label_close_ms <= self.groups[-1][0]:
            raise ValueError("duplicate_or_out_of_order_training_date")
        if not items:
            return
        for row in items:
            if len(row["x"]) != D or len(row["ys"]) != 2 or not all(
                math.isfinite(v) for v in row["x"]+row["ys"]):
                raise ValueError("invalid_learning_sample")
        self._apply(items, 1.)
        self.groups.append((label_close_ms, items))
        cutoff = asof_ms-self.config["training_window_days"]*DAY
        while self.groups and self.groups[0][0] < cutoff:
            _,old = self.groups.popleft()
            self._apply(old,-1.)
        self.max_label_close_ms = label_close_ms

    def fit(self, asof_ms: int, force: bool = False) -> bool:
        dates, samples = len(self.groups), sum(len(x[1]) for x in self.groups)
        if dates < self.config["min_training_dates"] or samples < self.config["min_training_samples"]:
            return False
        if self.max_label_close_ms is not None and self.max_label_close_ms > asof_ms:
            raise ValueError("future_label_in_model")
        if not force and self.fit_ms is not None and asof_ms-self.fit_ms < self.config["refit_days"]*DAY:
            return False
        matrix = [row[:] for row in self.xtx]
        for i in range(D):
            matrix[i][i] += self.config["ridge_alpha"]*(.01 if i==0 else 1.)
        self.weights = [solve(matrix,y) for y in self.xty]
        self.fit_ms,self.fit_dates,self.fit_samples = asof_ms,dates,samples
        return True

    def predict(self, x: list[float]) -> tuple[float,float] | None:
        if self.weights is None:
            return None
        return tuple(sum(a*b for a,b in zip(w,x)) for w in self.weights)

    def metadata(self) -> dict:
        state = {"type":"RIDGE_28D_NET_AND_BTC_EXCESS_LOG_MARKOUT", "features":FEATURE_NAMES,
                 "fit_ms":self.fit_ms, "max_label_close_ms":self.max_label_close_ms,
                 "fit_dates":self.fit_dates, "fit_samples":self.fit_samples,
                 "weights":self.weights, "independent_samples":False,
                 "calibrated_probability":False}
        state["model_sha256"] = hashlib.sha256(json_text(state).encode()).hexdigest()
        return state


class HistoryPanel:
    """Validated daily histories; access is always explicitly time-bounded."""
    def __init__(self, histories: dict[str,list[Bar]], config: dict):
        self.h = histories
        self.config = config
        self.index = {s:{b.open_ms:i for i,b in enumerate(bs)} for s,bs in histories.items()}
        self.cache: dict[tuple[str,int],dict|None] = {}

    def bar(self, symbol: str, day: int) -> Bar | None:
        i = self.index.get(symbol,{}).get(day)
        return None if i is None else self.h[symbol][i]

    def features_on(self, day: int) -> list[dict]:
        bi = self.index.get("BTCUSDT",{}).get(day)
        if bi is None:
            return []
        btc = self.h["BTCUSDT"][:bi+1]
        out=[]
        for s,bs in self.h.items():
            if s=="BTCUSDT" or s in self.config.get("excluded_symbols",[]):
                continue
            i=self.index[s].get(day)
            if i is None:
                continue
            key=(s,day)
            if key not in self.cache:
                self.cache[key]=feature(s,bs[:i+1],btc)
            r=self.cache[key]
            if r:
                out.append(r)
        return out

    def matured_group(self, outcome_day: int) -> list[dict]:
        horizon=self.config["horizon_days"]
        signal_day=outcome_day-(horizon+1)*DAY
        entry_day=signal_day+2*DAY
        be,bx=self.bar("BTCUSDT",entry_day),self.bar("BTCUSDT",outcome_day)
        if not be or not bx:
            return []
        btc_log=math.log(bx.close/be.open)
        # Entry and exit fee/slippage assumptions are the same as the paper broker.
        fee,slip=self.config["fee_rate"],self.config["slippage_rate"]
        cost_log=math.log((1+slip)*(1+fee)/((1-slip)*(1-fee)))
        out=[]
        for r in self.features_on(signal_day):
            e,x=self.bar(r["symbol"],entry_day),self.bar(r["symbol"],outcome_day)
            if not e or not x:
                continue
            logret=math.log(x.close/e.open)
            out.append({"x":r["x"],"ys":[clip(logret-cost_log,-1,1),clip(logret-btc_log-cost_log,-1,1)]})
        return out

    def warm_model(self, through_day: int) -> MaturedRidge:
        model=MaturedRidge(self.config)
        days=[b.open_ms for b in self.h.get("BTCUSDT",[]) if b.open_ms<=through_day]
        # Discard training labels older than the rolling window before fitting.
        for d in days:
            if d < through_day-self.config["training_window_days"]*DAY:
                continue
            rows=self.matured_group(d)
            model.learn(d+DAY-1,rows,d+DAY-1)
        model.fit(through_day+DAY-1,force=True)
        return model
