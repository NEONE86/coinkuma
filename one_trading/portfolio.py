"""Deterministic spot paper broker, shared by replay and forward-paper runner.

No exchange account/order endpoint exists. Fees and slippage are assumptions.
Hard risk limits govern *new* exposure, not a guarantee against gaps/outages.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import hashlib
import math
import re
from .observer import DAY, Bar, json_text


def validate_config(c: dict) -> None:
    if c.get("mode") != "EXPERIMENTAL_PAPER_ONLY":
        raise ValueError("only_experimental_paper_mode_is_implemented")
    for k,v in c.items():
        if isinstance(v,(int,float)) and not isinstance(v,bool) and not math.isfinite(v):
            raise ValueError("nonfinite_config:"+k)
    fractions = ["fee_rate","slippage_rate","max_position_weight","max_gross_weight",
                 "probe_risk","confirmed_risk","max_open_risk","daily_loss_limit","drawdown_halt"]
    for k in fractions:
        v=c[k]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0 <= v < 1:
            raise ValueError("invalid_config:"+k)
    for k in ("initial_cash","min_order_notional","ridge_alpha"):
        if not isinstance(c[k],(int,float)) or isinstance(c[k],bool) or not math.isfinite(c[k]) or c[k]<=0:
            raise ValueError("invalid_config:"+k)
    if not 0<c["probe_risk"]<=c["confirmed_risk"]<=c["max_open_risk"]:
        raise ValueError("invalid_risk_order")
    if type(c["max_positions"]) is not int or not 1<=c["max_positions"]<=20 or c["max_gross_weight"]>1:
        raise ValueError("invalid_exposure")
    if not 1<=c["scan_interval_seconds"]<=86400:
        raise ValueError("invalid_scan_interval")
    for k in ("horizon_days","min_training_dates","min_training_samples","training_window_days","refit_days"):
        if type(c[k]) is not int or c[k]<1:
            raise ValueError("invalid_config:"+k)


@dataclass
class Position:
    symbol: str
    qty: float
    cost_basis: float
    opened_ms: int
    stop: float
    initial_stop: float
    average_entry: float
    took_profit: bool = False


@dataclass
class Book:
    cash: float
    positions: dict[str,Position] = field(default_factory=dict)
    peak: float = 0.
    day_start_equity: float = 0.
    current_day: int = -1
    halted: bool = False
    paused_day: int = -1
    total_fees: float = 0.
    realized_pnl: float = 0.
    fills: list[dict] = field(default_factory=list)
    last_plan_id: str | None = None
    processed_plan_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, x: dict) -> 'Book':
        x=dict(x)
        x["positions"]={s:Position(**p) for s,p in x.get("positions",{}).items()}
        return cls(**x)

    def prices_ok(self, prices: dict[str,float]) -> None:
        for s in self.positions:
            if s not in prices or not math.isfinite(prices[s]) or prices[s]<=0:
                raise ValueError("held_price_missing_or_invalid:"+s)

    def equity(self, prices: dict[str,float]) -> float:
        self.prices_ok(prices)
        return self.cash+sum(p.qty*prices[s] for s,p in self.positions.items())

    def open_risk(self, prices: dict[str,float], c: dict) -> float:
        self.prices_ok(prices)
        return sum(p.qty*max(0.,prices[s]*(1+c["fee_rate"])-p.stop*(1-c["slippage_rate"])*(1-c["fee_rate"]))
                   for s,p in self.positions.items())

    def begin_day(self, ts: int, prices: dict[str,float]) -> None:
        eq=self.equity(prices)
        if self.current_day != ts//DAY:
            self.current_day=ts//DAY
            # The runtime records the previous mark as the daily reference; replay
            # explicitly calls this before the day's open to include overnight gaps.
            self.day_start_equity=eq
        self.peak=max(self.peak,eq)

    def risk_trigger(self, ts: int, prices: dict[str,float], c: dict) -> str | None:
        eq=self.equity(prices)
        self.peak=max(self.peak,eq)
        if self.peak and eq/self.peak-1 <= -c["drawdown_halt"]:
            self.halted=True
        if self.day_start_equity and eq/self.day_start_equity-1 <= -c["daily_loss_limit"]:
            self.paused_day=ts//DAY
        return "DRAWDOWN_HALT" if self.halted else "DAILY_LOSS_HALT" if self.paused_day==ts//DAY else None

    def _fill(self, side: str, s: str, qty: float, mark: float, ts: int, c: dict, reason: str) -> dict:
        price=mark*(1+c["slippage_rate"] if side=="BUY" else 1-c["slippage_rate"])
        fee=qty*price*c["fee_rate"]
        row={"ts_ms":ts,"mode":"SIMULATED_FILL","symbol":s,"side":side,"quantity":qty,
             "reference_price":mark,"fill_price":price,"fee":fee,"notional":qty*price,
             "reason":reason,"realized_pnl":None}
        self.total_fees+=fee
        self.fills.append(row)
        return row

    def sell(self, s: str, qty: float, mark: float, ts: int, c: dict, reason: str) -> None:
        if s not in self.positions:
            return
        p=self.positions[s]
        if not math.isfinite(mark) or mark<=0 or not math.isfinite(qty) or qty<=0:
            raise ValueError("invalid_sell")
        qty=min(qty,p.qty)
        cost=p.cost_basis*qty/p.qty
        row=self._fill("SELL",s,qty,mark,ts,c,reason)
        proceeds=row["notional"]-row["fee"]
        self.cash+=proceeds
        pnl=proceeds-cost
        self.realized_pnl+=pnl
        row["realized_pnl"]=pnl
        row["opened_ms"]=p.opened_ms
        p.qty-=qty
        p.cost_basis-=cost
        if p.qty<=1e-10:
            del self.positions[s]

    def liquidate(self, prices: dict[str,float], ts: int, c: dict, reason: str) -> None:
        self.prices_ok(prices)
        for s in list(self.positions):
            self.sell(s,self.positions[s].qty,prices[s],ts,c,reason)

    def monitor_quotes(self, prices: dict[str,float], ts: int, c: dict) -> None:
        """Forward paper: stop/TP fills at observed quote, never an earlier candle."""
        self.prices_ok(prices)
        for s in list(self.positions):
            p=self.positions[s]
            mark=prices[s]
            if mark<=p.stop:
                self.sell(s,p.qty,mark,ts,c,"OBSERVED_STOP")
            elif not p.took_profit and mark>=p.average_entry+c["profit_take_R"]*(p.average_entry-p.initial_stop):
                self.sell(s,p.qty/2,mark,ts,c,"OBSERVED_PARTIAL_TP")
                if s in self.positions:
                    self.positions[s].took_profit=True
        reason=self.risk_trigger(ts,prices,c)
        if reason:
            self.liquidate(prices,ts,c,reason)

    def monitor_bar(self, bars: dict[str,Bar], ts: int, c: dict) -> None:
        """Historical fill convention: stop-first when both stop and TP are hit."""
        for s in list(self.positions):
            if s not in bars:
                raise ValueError("held_bar_missing:"+s)
            p,b=self.positions[s],bars[s]
            if b.low<=p.stop:
                # A gap through a stop fills at the worse open, not at the stop.
                self.sell(s,p.qty,min(b.open,p.stop),ts,c,"BAR_STOP_FIRST")
            elif not p.took_profit:
                target=p.average_entry+c["profit_take_R"]*(p.average_entry-p.initial_stop)
                if b.high>=target:
                    self.sell(s,p.qty/2,target,ts,c,"BAR_PARTIAL_TP")
                    if s in self.positions:
                        self.positions[s].took_profit=True
        prices={s:b.close for s,b in bars.items()}
        reason=self.risk_trigger(ts,prices,c)
        if reason:
            self.liquidate(prices,ts,c,reason)

    def apply_plan(self, plan: dict, prices: dict[str,float], ts: int, c: dict) -> list[str]:
        """Sell before buy; all sizing is recomputed at the execution quote."""
        if plan["id"] in self.processed_plan_ids:
            return ["DUPLICATE_PLAN_IGNORED"]
        if ts<plan["execute_after_ms"]:
            raise ValueError("cannot_execute_before_due_time")
        if ts>plan["expires_ms"]:
            self.last_plan_id=plan["id"]; self.processed_plan_ids.append(plan["id"])
            return ["EXPIRED_PLAN_DISCARDED"]
        context=plan.get("position_context")
        if not isinstance(context,dict) or any(
            not isinstance(s,str) or type(opened) is not int
            for s,opened in context.items()
        ):
            raise ValueError("position_context_required_for_delayed_plan")
        self.prices_ok(prices)
        notes=[]
        targets={r["symbol"]:r for r in plan["targets"]}
        eq=self.equity(prices)
        trigger=self.risk_trigger(ts,prices,c)
        if trigger:
            self.liquidate(prices,ts,c,trigger)
            self.last_plan_id=plan["id"]; self.processed_plan_ids.append(plan["id"])
            return [trigger]
        for s in list(self.positions):
            p=self.positions[s]
            # A queued plan may predate this entry or refer to an older trade
            # in the same symbol. Absence from that plan is not an exit intent.
            if context.get(s)!=p.opened_ms:
                notes.append(s+":POSITION_NOT_OBSERVED_BY_PLAN")
                continue
            t=targets.get(s)
            if not t or t["target_weight"]<=0:
                self.sell(s,p.qty,prices[s],ts,c,"ROTATE_OR_INVALIDATE")
                continue
            # Stops only tighten, and only using prior completed-bar information.
            p.stop=max(p.stop,min(t["stop"],prices[s]*.999))
            excess=p.qty*prices[s]-eq*t["target_weight"]
            if excess>max(c["min_order_notional"],.02*eq):
                self.sell(s,excess/prices[s],prices[s],ts,c,"TRIM_TARGET")
        for t in plan["targets"]:
            s=t["symbol"]
            if not t.get("allow_add",False) or t["target_weight"]<=0:
                continue
            p=self.positions.get(s)
            if (s in context and (p is None or p.opened_ms!=context[s])) or (
                s not in context and p is not None
            ):
                # Do not turn an add into a new entry after a stop, or a
                # previously planned new entry into an unplanned add.
                notes.append(s+":STALE_POSITION_INTENT_SKIPPED")
                continue
            if s not in prices or not math.isfinite(prices[s]) or prices[s]<=0:
                notes.append(s+":QUOTE_MISSING"); continue
            mark=prices[s]
            if mark>t["reference_price"]*1.03:
                notes.append(s+":ENTRY_GAP_TOO_HIGH"); continue
            stop=t["stop"]
            if stop<=0 or stop>=mark or mark<t["reference_price"]*.90:
                notes.append(s+":ENTRY_INVALIDATED"); continue
            p=self.positions.get(s)
            if p and p.took_profit:
                continue
            if not p and len(self.positions)>=c["max_positions"]:
                continue
            eq=self.equity(prices)
            gross=sum(v.qty*prices[k] for k,v in self.positions.items())
            current=p.qty*mark if p else 0.
            target_notional=eq*min(t["target_weight"],c["max_position_weight"])
            desired=max(0.,target_notional-current)
            desired=min(desired,max(0.,eq*c["max_gross_weight"]-gross),self.cash)
            fill_price=mark*(1+c["slippage_rate"])
            cost_per_unit=fill_price*(1+c["fee_rate"])
            effective_stop=max(stop,p.stop) if p else stop
            risk_per_unit=cost_per_unit-effective_stop*(1-c["slippage_rate"])*(1-c["fee_rate"])
            current_risk=(p.qty*max(0.,mark*(1+c["fee_rate"])-effective_stop*(1-c["slippage_rate"])*(1-c["fee_rate"]))) if p else 0.
            individual=max(0.,eq*min(t["risk_fraction"],c["confirmed_risk"])-current_risk)
            aggregate=max(0.,eq*c["max_open_risk"]-self.open_risk(prices,c))
            qty=min(desired/cost_per_unit,self.cash/cost_per_unit,
                    individual/max(risk_per_unit,1e-12),aggregate/max(risk_per_unit,1e-12))
            if qty*fill_price<c["min_order_notional"]:
                continue
            row=self._fill("BUY",s,qty,mark,ts,c,("ADD_CONFIRMED" if t.get("stage")=="CONFIRMED" else "TOP_UP_PROBE") if p else "OPEN_PROBE")
            cost=row["notional"]+row["fee"]
            self.cash-=cost
            if p:
                p.average_entry=(p.average_entry*p.qty+fill_price*qty)/(p.qty+qty)
                p.qty+=qty; p.cost_basis+=cost; p.stop=effective_stop
            else:
                self.positions[s]=Position(s,qty,cost,ts,stop,stop,fill_price)
            if self.cash < -1e-7:
                raise AssertionError("negative_cash")
        self.last_plan_id=plan["id"]; self.processed_plan_ids.append(plan["id"])
        return notes


def make_plan(rows: list[dict], model, book: Book, prices: dict[str,float],
              observed_ms: int, c: dict) -> dict:
    """Weekly/monthly context -> learned expectations -> probe/add/hold/rotate.

    An extended new asset is rejected; extension alone never sells an existing
    winner. A cheap quote or large historical drawdown alone cannot trigger entry.
    """
    decisions=[]
    choices=[]
    rowmap={r["symbol"]:r for r in rows}
    eq=book.equity(prices)
    if set(book.positions)-set(rowmap):
        raise ValueError("HELD_FEATURES_MISSING")
    for r in rows:
        s=r["symbol"]; m=r["metrics"]; p=book.positions.get(s)
        pred=model.predict(r["x"])
        d={"symbol":s,"state":"WAIT","reasons":[],"metrics":m,
           "prediction":list(pred) if pred else None,"support_stop":r["support_stop"]}
        decisions.append(d)
        if pred is None:
            d["reasons"]=["MODEL_WARMUP"]; continue
        net,excess=pred
        riskoff=m["BTC_regime"]=="RISK_OFF"
        extended=(m["return_7d"]>c["max_buy_extension_7d"] or
                  m["return_30d"]>c["max_buy_extension_30d"] or
                  m["extension_above_SMA20"]>c["max_buy_extension_sma20"])
        held_days=(observed_ms-p.opened_ms)/DAY if p else 0
        stop=r["support_stop"]
        price=prices.get(s,m["last_close"])
        riskfrac=(price-stop)/price
        if p:
            stop=max(stop,p.stop)
            if riskoff or held_days>=c["max_hold_days"] or (
                held_days>=c["min_hold_days"] and (net<0 or excess<-.02)):
                d["state"]="EXIT";d["reasons"]=["THESIS_INVALIDATED_OR_TIME_LIMIT"];continue
            d["state"]="HOLD"
            d["reasons"]=["HOLD_WINNER_NOT_CHASE_NEW_ENTRY"]
            choices.append((net+excess+.03,s,r,d,stop,not extended,held_days))
            continue
        if riskoff:
            d["state"]="REJECT"; d["reasons"]=["RISK_OFF"]; continue
        if extended:
            d["state"]="LATE"; d["reasons"]=["NO_SURGE_CHASING"]; continue
        if not m["holding_low"] or m["base_width_20d"]>.40:
            d["reasons"]=["BASE_NOT_STABLE"]; continue
        if not c["min_entry_risk_fraction"]<=riskfrac<=c["max_entry_risk_fraction"]:
            d["reasons"]=["STOP_DISTANCE_NOT_USABLE"]; continue
        if net<c["min_predicted_net_log_return"] or excess<c["min_predicted_excess_log_return"]:
            d["reasons"]=["MODEL_EXPECTATION_TOO_LOW"];continue
        d["state"]="PROBE_CANDIDATE";d["reasons"]=["STABLE_BASE_WITH_POSITIVE_LEARNED_EXPECTATION"]
        choices.append((net+excess,s,r,d,stop,True,0.))
    # A young position is protected from ranking churn, but not from invalidation.
    choices.sort(key=lambda v:(book.positions.get(v[1]) is not None and v[6]<c["min_hold_days"],v[0],v[1]),reverse=True)
    selected=choices[:c["max_positions"]]
    targets=[]
    for _,s,r,d,stop,allow_add,held_days in selected:
        p=book.positions.get(s)
        price=prices.get(s,r["metrics"]["last_close"])
        confirmed=bool(p and held_days>=c["min_hold_days"] and price>=p.average_entry and
                       d["prediction"][0]>=c["min_predicted_net_log_return"])
        risk=c["confirmed_risk"] if confirmed else c["probe_risk"]
        width=max((price-stop)/price,c["min_entry_risk_fraction"])
        weight=min(c["max_position_weight"],risk/width)
        if p and (p.took_profit or not allow_add):
            weight=min(weight,p.qty*price/eq)
        if p and allow_add and confirmed:
            d["state"]="ADD_OR_HOLD"
        targets.append({"symbol":s,"target_weight":weight,"stop":stop,
                        "risk_fraction":risk,"reference_price":price,"allow_add":allow_add,"stage":"CONFIRMED" if confirmed else "PROBE"})
    total=sum(t["target_weight"] for t in targets)
    if total>c["max_gross_weight"]:
        for t in targets:t["target_weight"]*=c["max_gross_weight"]/total
    for d in decisions:
        if d["state"]=="PROBE_CANDIDATE" and d["symbol"] not in {t["symbol"] for t in targets}:
            d["state"]="WAIT_CAPACITY"
    # Actual observation today -> no retroactive next-open fill today.
    due=(observed_ms//DAY+1)*DAY
    plan={"observed_ms":observed_ms,"execute_after_ms":due,"expires_ms":due+DAY-1,
          "position_context":{s:p.opened_ms for s,p in book.positions.items()},
          "targets":targets,"decisions":decisions,"model":model.metadata(),
          "fundamental_revaluation_evidence":"NOT_CONNECTED",
          "mode":"EXPERIMENTAL_PAPER_PLAN"}
    plan["id"]=hashlib.sha256(json_text(plan).encode()).hexdigest()
    return plan
