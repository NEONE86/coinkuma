"""Secondary cash/fill reconciliation independent of the strategy selection loop.

Does not establish exchange execution accuracy. Missing/forged events are also
checked by deterministic decision replay at the CLI boundary.
"""
from __future__ import annotations

from .market import Market
from .replay import Limits


def reconcile(m:Market, fold:tuple[int,int], limits:Limits,
              events:list[dict], equity_points:list[list], expected_summary:dict)->dict:
    start,stop=fold
    wallet=limits.starting_cash
    held={s:0.0 for s in m.symbols}
    marks={s:m.series[s][start].o for s in m.symbols}
    event_at:dict[tuple[str,str],list[dict]]={}
    for event in events:
        key=(event["t"],event["event"])
        event_at.setdefault(key,[]).append(event)
    checked=0;totcost=0.0;total_funding=0.0
    for j in range(start,stop):
        t=m.times[j].isoformat()
        if j-start>=len(equity_points):break
        for s in m.symbols:
            x=m.series[s][j]
            wallet+=held[s]*(x.o-marks[s]);marks[s]=x.o
        for ev in event_at.get((t,"FILL"),[]):
            s=ev["s"]
            if s not in held:raise ValueError("AUDIT_INVALID_SYMBOL")
            if abs(held[s]-ev["old_qty"])>1e-8:raise ValueError("AUDIT_OLD_QTY_MISMATCH")
            op=m.series[s][j].o
            if abs(op-ev["price"])>1e-9*max(1,op):raise ValueError("AUDIT_FILL_NOT_NEXT_OPEN")
            cost_rate=(limits.fee_bps+limits.slippage_bps)/10000+m.series[s][j].extras.get("spread_bps",0)/20000
            required=abs(ev["new_qty"]-held[s])*op*cost_rate
            if abs(required-ev["cost"])>1e-7*max(1,required):raise ValueError("AUDIT_COST_INCONSISTENT")
            wallet-=required;totcost+=required;held[s]=ev["new_qty"];checked+=1
        total_gross=sum(abs(held[s]*marks[s]) for s in m.symbols)
        if wallet>0 and total_gross > (wallet+totcost*.5)*limits.max_gross_fraction*1.03:
            # Account drawdown can cause drift; only a broad guard is possible
            # without independently replaying the pre-trade sizing algorithm.
            raise ValueError("AUDIT_GROSS_EXPOSURE_BREACH")
        for s in m.symbols:
            x=m.series[s][j]
            wallet+=held[s]*(x.c-marks[s]);marks[s]=x.c
            payment=held[s]*x.c*x.extras.get("funding_rate",0.)
            wallet-=payment;total_funding-=payment
        expected=equity_points[j-start]
        if expected[0]!=t:raise ValueError("AUDIT_EQUITY_TIMESTAMP")
        # FINAL_FLAT occurs after the last bar close
        if j==stop-1:
            for ev in event_at.get((t,"FINAL_FLAT"),[]):
                s=ev["s"]
                if s not in held or abs(held[s]-ev["old_qty"])>1e-8:
                    raise ValueError("AUDIT_FINAL_QTY_MISMATCH")
                p=marks[s]
                cost=abs(held[s])*p*((limits.fee_bps+limits.slippage_bps)/10000 +m.series[s][j].extras.get("spread_bps",0)/20000)
                if abs(cost-ev["cost"])>1e-7*max(1,cost):raise ValueError("AUDIT_FINAL_COST_MISMATCH")
                wallet-=cost;totcost+=cost;held[s]=0;checked+=1
        if abs(wallet-float(expected[1]))>1e-6*max(1,abs(wallet)):
            raise ValueError(f"AUDIT_EQUITY_RECONCILIATION:{t}")
    if abs(wallet-expected_summary["final_equity"])>1e-6*max(1,abs(wallet)):
        raise ValueError("AUDIT_FINAL_BALANCE_MISMATCH")
    if abs(totcost-expected_summary["costs"])>1e-6*max(1,totcost):raise ValueError("AUDIT_TOTAL_COST_MISMATCH")
    if abs(total_funding-expected_summary["funding_pnl"])>1e-6*max(1,abs(total_funding)):
        raise ValueError("AUDIT_FUNDING_MISMATCH")
    if checked!=expected_summary["fills"]:raise ValueError("AUDIT_FILL_COUNT_MISMATCH")
    return {"status":"PASS_ACCOUNTING_RECONCILIATION_ONLY","checked_fills":checked,
            "recomputed_equity":wallet,"recomputed_cost":totcost,"recomputed_funding_pnl":total_funding}
