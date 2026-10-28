"""One shared outcome per question; only resolved, available outcomes are scored."""
from .store import utc,instant,digest
from .market import endpoint_price,number

def resolve_values(question,prices,resolved_at):
    if instant(resolved_at)<instant(question["deadline"]):raise ValueError("IMMATURE")
    universe=question["universe"]
    if set(prices)!=set(universe) or set(question["origin_prices"])!=set(universe):raise ValueError("outcome_universe")
    for symbol,p in prices.items():
        number(p["price"],True)
        if p["symbol"]!=symbol or p["endpoint"]!=question["deadline"]:raise ValueError("endpoint_identity")
        if not instant(question["deadline"])<=instant(p["received_at"])<=instant(resolved_at):raise ValueError("endpoint_clock")
    origin=question["origin_prices"]
    for s in universe:number(origin[s],True)
    returns={s:prices[s]["price"]/origin[s]-1 for s in universe}
    symbol=question["symbol"];others=[r for s,r in returns.items() if s!=symbol]
    if not others:raise ValueError("benchmark_missing")
    benchmark=sum(others)/len(others)
    gross=returns[symbol];net=(1+gross)*(1-question["assumed_cost_per_side"])/(1+question["assumed_cost_per_side"])-1
    stress=(1+gross)*(1-question["stress_cost_per_side"])/(1+question["stress_cost_per_side"])-1
    excess=net-benchmark
    return dict(question_id=question["id"],symbol=symbol,horizon_hours=question["horizon_hours"],
                status="RESOLVED",resolved_at=resolved_at,deadline=question["deadline"],
                gross_return=gross,net_signal_return=net,benchmark_return=benchmark,
                excess_return=excess,stress_excess_return=stress-benchmark,label=excess>0,
                outcome_type="ASSUMPTION_BASED_SIGNAL_MARKOUT_NOT_REALIZED_PNL",prices=prices)

def mature(store):
    now=utc();cache={}
    for q in store.all("question"):
        if store.get("outcome",q["id"],True) or instant(now)<instant(q["deadline"]):continue
        try:
            prices={}
            for s in q["universe"]:
                key=(s,q["deadline"])
                if key not in cache:cache[key]=endpoint_price(store,s,q["deadline"])
                prices[s]=cache[key]
            result=resolve_values(q,prices,utc())
            store.append("outcome",q["id"],result)
        except Exception as exc:
            store.failure("outcome_pending",exc,question_id=q["id"],status="DATA_MISSING_NOT_SCORED")

def compare(store,config):
    outcomes={r["question_id"]:r for r in store.all("outcome") if r["status"]=="RESOLVED"}
    policies={}
    for policy in config["policies"]:
        forecast=[];selected=[]
        for d in store.all("policy_decision"):
            if d["policy"]!=policy:continue
            for qid in d["question_ids"]:
                if qid not in outcomes:continue
                o=outcomes[qid];p=d["probabilities"][str(o["horizon_hours"])]
                if p is None:continue
                forecast.append((p-int(o["label"]))**2)
                if d["action"]=="LONG":selected.append(o)
        policies[policy]=dict(resolved_forecasts=len(forecast),brier=sum(forecast)/len(forecast) if forecast else None,
                    selected_resolved=len(selected),
                    mean_net_excess=sum(o["excess_return"] for o in selected)/len(selected) if selected else None,
                    mean_stress_excess=sum(o["stress_excess_return"] for o in selected)/len(selected) if selected else None)
    decisions=store.all("policy_decision")
    paired=[]
    for qid,o in outcomes.items():
        participants=[d for d in decisions if qid in d["question_ids"] and d["probabilities"][str(o["horizon_hours"])] is not None]
        if {d["policy"] for d in participants}==set(config["policies"]):paired.append(qid)
    return dict(as_of=utc(),policies=policies,common_resolved_questions=len(paired),
                pending_questions=len(store.all("question"))-len(outcomes),
                independent_sample_status="OVERLAPPING_HORIZONS_NOT_INDEPENDENT",
                champion=config["champion"],promotion="DISABLED_PREREGISTERED_FORWARD_ONLY",
                edge_status="EDGE_NOT_PROVEN",live_trading_enabled=False)
