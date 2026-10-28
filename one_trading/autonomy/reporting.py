"""Separate software, scheduler, forecast and economic evidence judgments."""
import json,os,subprocess
from .store import utc,instant
from .paper import account

def scheduler_status(root):
    if os.name!="nt":return {"active":False,"status":"SCHEDULER_NOT_ACTIVATED_ON_THIS_PLATFORM"}
    command="""$ErrorActionPreference='Stop';$t=Get-ScheduledTask -TaskName 'ONE-DualEngine-Forward-v1-20261029';$i=Get-ScheduledTaskInfo -TaskName $t.TaskName;[pscustomobject]@{state=$t.State.ToString();enabled=$t.Settings.Enabled;last_result=$i.LastTaskResult;last_run_utc=$i.LastRunTime.ToUniversalTime().ToString('o');next_run_utc=$i.NextRunTime.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress"""
    try:
        r=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],
             capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=10,
             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if r.returncode:raise ValueError("scheduler_query_denied_or_missing")
        result=json.loads(r.stdout);recent=(instant(utc())-instant(result["last_run_utc"])).total_seconds()
        result["active"]=bool(result["enabled"] and result["state"]!="Disabled" and -120<=recent<1200 and result["last_result"] in (0,267009))
        result["status"]="FORWARD_RUNNING" if result["active"] else "SCHEDULER_NOT_HEALTHY"
        return result
    except Exception as exc:return {"active":False,"status":"SCHEDULER_STATUS_UNAVAILABLE","error":str(exc)}

def diagnostics(store,config):
    outcomes={o["question_id"]:o for o in store.all("outcome") if o["status"]=="RESOLVED"}
    decisions=store.all("policy_decision");shared=[]
    for qid,o in outcomes.items():
        rows=[d for d in decisions if qid in d["question_ids"]]
        if len(rows)==3 and all(d["probabilities"][str(o["horizon_hours"])] is not None for d in rows):shared.append((qid,o,rows))
    by_horizon={}
    for h in config["horizons_hours"]:
        cases=[x for x in shared if x[1]["horizon_hours"]==h]
        by_horizon[str(h)]={}
        for policy in config["policies"]:
            selected=[];brier=[];bins={str(i):[] for i in range(5)};contributions=[]
            for qid,o,rows in cases:
                d=next(x for x in rows if x["policy"]==policy);p=d["probabilities"][str(h)]
                brier.append((p-int(o["label"]))**2);bins[str(min(4,int(p*5)))].append((p,int(o["label"])))
                contributions.append((o["net_signal_return"] if d["action"]=="LONG" else 0)*config["risk_fraction_per_position"])
                if d["action"]=="LONG":selected.append(o["net_signal_return"])
            by_horizon[str(h)][policy]=dict(common_questions=len(cases),
                 mean_equal_risk_signal_contribution=sum(contributions)/len(contributions) if contributions else None,
                 brier=sum(brier)/len(brier) if brier else None,
                 worst_selected_markout=min(selected) if selected else None,
                 calibration=[dict(bin=k,n=len(v),mean_p=sum(a for a,b in v)/len(v) if v else None,
                                   event_rate=sum(b for a,b in v)/len(v) if v else None) for k,v in bins.items()],
                 status="SIGNAL_DIAGNOSTIC_NOT_EXECUTED_PORTFOLIO")
    independent=[];last_end=None
    questions={q["id"]:q for q in store.all("question")}
    for _,o,_ in sorted(shared,key=lambda x:questions[x[0]]["registered_at"]):
        q=questions[o["question_id"]]
        if q["horizon_hours"]!=72:continue
        if last_end is None or instant(q["registered_at"])>=last_end:
            independent.append(q["id"]);last_end=instant(q["deadline"])
    accounts={p:account(store,config,p) for p in config["policies"]}
    entries=store.all("paper_entry");exits=store.all("paper_exit")
    risk={}
    for policy in config["policies"]:
        curve=[r["accounts"][policy]["equity_krw"] for r in store.all("run_complete") if policy in r.get("accounts",{}) and r["accounts"][policy].get("equity_status")!="COST_CARRY_NOT_MARKED"]
        peak=config["engine_capital_krw"]["CASHFLOW"];drawdown=0.
        for value in curve:
            peak=max(peak,value);drawdown=max(drawdown,1-value/peak)
        closed=[x for x in exits if x["policy"]==policy]
        returns=sorted(x["realized_pnl_krw"]/(-next(e["cash_delta_krw"] for e in entries if e["id"]==x["id"])) for x in closed)
        risk[policy]=dict(marked_max_drawdown=drawdown if any(e["policy"]==policy for e in entries) else None,
            closed_trades=len(closed),worst_realized_trade_return=min(returns) if returns else None,
            tail_5pct_mean=sum(returns[:max(1,len(returns)//20)])/max(1,len(returns)//20) if len(returns)>=20 else None,
            evidence_status="INSUFFICIENT_FOR_RISK_OR_EDGE_CLAIM")
    cf_open=accounts[config["champion"]]["open_positions"];combined={}
    for p in cf_open:combined[p["symbol"]]=combined.get(p["symbol"],0)+p["cost_krw"]
    growth_positions=[p for e in store.all("growth_experiment") for p in e["paper_positions"]]
    for p in growth_positions:combined[p["symbol"]]=combined.get(p["symbol"],0)+p["cost_krw"]
    return dict(as_of=utc(),paired_by_horizon=by_horizon,risk=risk,nonoverlapping_72h_market_episodes=len(independent),
         capital=dict(total_cap_krw=config["total_capital_krw"],allocations=config["engine_capital_krw"],
            active_cf_champion=config["champion"],active_growth_champion=config["growth_champion"],
            shadow_policy_capital_is_not_summed=True,combined_open_asset_cost_krw=combined,
            CASHFLOW=accounts[config["champion"]],
            CAPITAL_GROWTH=dict(cash_krw=config["engine_capital_krw"]["CAPITAL_GROWTH"],positions=growth_positions,
                                realized_pnl_krw=0,withdrawals_krw=0),
            actual_withdrawals_krw=0),
         paper=dict(entries=len(entries),exits=len(exits),fees_usdt=sum(x["fee_usdt"] for x in entries+exits),
            realized_pnl_by_policy={p:accounts[p]["realized_pnl_krw"] for p in config["policies"]},
            actual_exchange_orders=0,withdrawable_real_cashflow=0),
         caveats=["CF labels compare cost-net asset markout against gross other-asset benchmark, a conservative hurdle.",
                  "Equal-risk markouts overlap; they are not an executable simultaneous portfolio.",
                  "Cross-regime and cross-asset transfer, tail risk and selection bias remain unproven.",
                  "Growth economic confirmation requires structured attributable evidence; publisher-page changes do not trigger entry.",
                  "No policy promotion; no real exchange execution or withdrawal."])
