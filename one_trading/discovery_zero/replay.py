"""Causal market simulator. Decisions on completed bar i execute at bar i+1 open.

No exchange API, synthetic fill claims or real futures liquidation handling.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .genome import Genome
from .market import Market


@dataclass(frozen=True)
class Limits:
    starting_cash: float = 10000.0
    fee_bps: float = 8.0
    slippage_bps: float = 7.0
    max_symbol_fraction: float = .75
    max_gross_fraction: float = 1.5
    hard_drawdown: float = .55

    def __post_init__(self)->None:
        if any(not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError("NONFINITE_LIMIT")
        if self.starting_cash <= 0 or self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("INVALID_COST_OR_CAPITAL")
        if not (0 < self.max_symbol_fraction <= self.max_gross_fraction <= 2.0):
            raise ValueError("INVALID_EXPOSURE_LIMIT")
        if not .05 <= self.hard_drawdown < 1:
            raise ValueError("INVALID_DRAWDOWN_LIMIT")


@dataclass
class Result:
    final_equity: float
    net_multiple: float
    log_growth: float
    max_drawdown: float
    turnover: float
    costs: float
    funding_pnl: float
    fills: int
    ruin: bool
    halted: bool
    observations: int
    equity_points: list[list[Any]]
    events: list[dict[str,Any]]

    def summary(self)->dict:
        d=asdict(self)
        del d["equity_points"]
        del d["events"]
        return d


def simulate(market:Market, genome:Genome, start:int, stop:int, limits:Limits,
             *, capture:bool=False)->Result:
    if start < 17 or stop > len(market.times) or stop-start < 2:
        raise ValueError("INVALID_EVALUATION_RANGE")
    positions={s:0.0 for s in market.symbols}
    entry_price={s:0.0 for s in market.symbols}
    entry_bar={s:start for s in market.symbols}
    marks={s:market.series[s][start].o for s in market.symbols}
    cash=limits.starting_cash
    peak=cash
    max_dd=0.0
    cum_cost=0.0
    funding_pnl=0.0
    turnover=0.0
    nfill=0
    halted=False
    ruin=False
    observations=0
    pending={s:("FLAT",0.0) for s in market.symbols}
    events=[]
    points=[]

    def record(d:dict)->None:
        if capture:events.append(d)

    for i in range(start,stop):
        t=market.times[i].isoformat()
        # Previous completed-bar decision becomes eligible at this new open.
        eq_open=cash
        for s in market.symbols:
            op=market.series[s][i].o
            cash+=positions[s]*(op-marks[s]);marks[s]=op
        eq_open=cash
        if eq_open<=0:
            ruin=True;halted=True;cash=0.0
            positions={s:0.0 for s in market.symbols}
            points.append([t,0.0])
            record({"event":"RUIN","t":t})
            break
        desired={}
        for s in market.symbols:
            action,size=pending[s]
            op=marks[s]
            if halted or action=="FLAT":q=0.0
            elif action=="HOLD":q=positions[s]
            elif action=="LONG":q=eq_open*min(limits.max_symbol_fraction,limits.max_symbol_fraction*size)/op
            elif action=="SHORT":q=-eq_open*min(limits.max_symbol_fraction,limits.max_symbol_fraction*size)/op
            else:raise AssertionError(action)
            desired[s]=q
        total_gross=sum(abs(desired[s]*marks[s]) for s in market.symbols)
        if total_gross>eq_open*limits.max_gross_fraction:
            factor=eq_open*limits.max_gross_fraction/total_gross
            desired={s:q*factor for s,q in desired.items()}
        for s in market.symbols:
            old=positions[s]; new=desired[s]; delta=new-old
            if abs(delta)<=1e-12:continue
            bar=market.series[s][i]
            cost_rate=(limits.fee_bps+limits.slippage_bps)/10000
            if "spread_bps" in bar.extras:
                cost_rate+=bar.extras["spread_bps"]/20000
            cost=abs(delta)*marks[s]*cost_rate
            notional=abs(delta)*marks[s]
            if old*new<=0 or abs(new)>abs(old)+1e-12:
                entry_bar[s]=i
                entry_price[s]=marks[s]
            positions[s]=new
            cash-=cost;cum_cost+=cost;turnover+=notional; nfill+=1
            record({"event":"FILL","t":t,"s":s,"price":marks[s],"old_qty":old,"new_qty":new,"cost":cost})
        # Price and funding settlement for this 4h candle. Rates in the input
        # are only applied at this candle's close; missing rates are disclosed.
        for s in market.symbols:
            bar=market.series[s][i]
            close=bar.c
            cash+=positions[s]*(close-marks[s]);marks[s]=close
            payment=positions[s]*close*bar.extras.get("funding_rate",0.)
            cash-=payment; funding_pnl-=payment
        if cash<=0:
            ruin=True;halted=True;cash=0.0
            positions={s:0.0 for s in market.symbols}
            record({"event":"RUIN","t":t})
        peak=max(peak,cash)
        dd=(peak-cash)/peak
        max_dd=max(max_dd,dd)
        points.append([t,round(cash,8)])
        if ruin:break
        if dd>=limits.hard_drawdown and not halted:
            halted=True
            record({"event":"RISK_HALT","t":t,"drawdown":dd})
        if i>=stop-1:break
        for s in market.symbols:
            x=dict(market.features[s][i]); p=positions[s]
            x["position"]=max(-3.,min(3.,p*market.series[s][i].c/max(1e-9,cash)))
            x["position_age"]=min(3.0,(i-entry_bar[s])/24.0) if p else 0.
            x["unrealized"]=max(-3.,min(3.,(market.series[s][i].c/entry_price[s]-1)*(1 if p>0 else -1)*10)) if p else 0.
            x["drawdown"]=dd
            pending[s]=("FLAT",0.) if halted else genome.action(x)
            observations+=1
            record({"event":"DECISION","t":t,"s":s,"action":pending[s][0],"size":round(pending[s][1],10),"feature_index":i})
    # Exit all positions at the last *completed* close. Avoid free liquidation.
    if not ruin:
        final_t=market.times[min(stop-1,len(market.times)-1)].isoformat()
        for s,p in positions.items():
            if abs(p)<1e-12:continue
            price=marks[s]
            cost_rate=(limits.fee_bps+limits.slippage_bps)/10000
            last_bar=market.series[s][stop-1]
            if "spread_bps" in last_bar.extras:cost_rate+=last_bar.extras["spread_bps"]/20000
            cost=abs(p)*price*cost_rate
            cash-=cost;cum_cost+=cost;turnover+=abs(p)*price;nfill+=1
            record({"event":"FINAL_FLAT","t":final_t,"s":s,"price":price,"old_qty":p,"cost":cost})
        if points:points[-1][1]=round(cash,8)
        peak=max(peak,cash)
        max_dd=max(max_dd,(peak-cash)/peak)
    growth=math.log(max(1e-300,cash/limits.starting_cash)) if cash>0 else -690.0
    return Result(cash,cash/limits.starting_cash,growth,max_dd,turnover/limits.starting_cash,
                  cum_cost,funding_pnl,nfill,ruin,halted,observations,points,events)
