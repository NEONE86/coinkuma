"""Three frozen policies evaluated on precisely the same observation packet."""
from datetime import timedelta
from .store import utc,instant,digest
from .validation import source_identity

POLICIES=("CF_BASE","CF_NUMERIC","CF_AI_MEMORY")

def common_context(config,packet):
    return dict(cutoff=packet["cutoff"],hour=packet["hour"],universe=packet["universe"],
                packet_hash=digest(packet),implementation_hash=source_identity(),capital_krw=config["engine_capital_krw"]["CASHFLOW"],
                risk_fraction=config["risk_fraction_per_position"],max_symbol_krw=config["max_symbol_krw"],
                cost_per_side=config["signal_markout_cost_per_side"],stress_cost_per_side=config["stress_cost_per_side"],
                horizons=config["horizons_hours"],instrument=config["instrument"],config_hash=digest(config))

def evaluate(config,packet,call):
    ctx=common_context(config,packet);context_hash=digest(ctx);out=[]
    ai={r["symbol"]:r for r in (call.get("answer") or {}).get("assets",[])}
    for policy in POLICIES:
        for symbol,row in packet["assets"].items():
            action="NO_TRADE";reason="NO_RELATIVE_DISLOCATION";prob={str(h):.5 for h in config["horizons_hours"]}
            price_ok=row["price_status"]=="VALID" and row.get("residual_1h") is not None
            numeric_ok=row.get("numeric_status")=="VALID" and row.get("taker_buy_fraction") is not None
            if price_ok and row["residual_1h"]<config["base_residual_threshold"]:
                if policy=="CF_BASE":
                    action="LONG";reason="NEGATIVE_RELATIVE_RETURN";prob={str(h):.55 for h in config["horizons_hours"]}
                elif policy=="CF_NUMERIC":
                    if not numeric_ok:reason="DATA_MISSING"
                    elif row["oi_quantity_change"]<config["numeric_oi_quantity_threshold"] and row["last_settled_funding_per_hour"]<=config["numeric_funding_hourly_max"] and row["taker_buy_fraction"]>=config["numeric_taker_buy_min"] and abs(row["basis"])<=.005:
                        action="LONG";reason="CONDITIONAL_POSITION_REDUCTION_REBOUND";prob={str(h):.58 for h in config["horizons_hours"]}
                    else:reason="NUMERIC_GATE_NOT_MET"
            if policy=="CF_AI_MEMORY":
                if call["status"]!="CONNECTED_VALID":reason=call["status"];prob={str(h):None for h in config["horizons_hours"]}
                else:
                    m=ai[symbol];action=m["action"];reason=m["rationale"]
                    prob={str(h):m["p_excess_"+str(h)+"h"] for h in config["horizons_hours"]}
                    if action=="LONG" and (m["p_excess_24h"]<.55 or m["classification"]!="FORCED_REDUCTION_HYPOTHESIS"):
                        action="NO_TRADE";reason="AI_EDGE_OR_CAUSAL_GATE_NOT_MET"
                    if not numeric_ok:action="NO_TRADE";reason="DATA_MISSING"
            if not price_ok:action="NO_TRADE";reason="DATA_MISSING"
            if price_ok and row["book"]["spread_bps"]>config["max_spread_bps"]:
                action="NO_TRADE";reason="SPREAD_LIMIT"
            if (instant(utc())-instant(packet["cutoff"])).total_seconds()>config["max_input_age_seconds"]:
                action="NO_TRADE";reason="STALE_AFTER_MODEL_OR_RESTART"
            out.append(dict(policy=policy,symbol=symbol,action=action,reason=reason,
                            probabilities=prob,context_hash=context_hash,context=ctx,
                            ai_call_id=call["id"] if policy=="CF_AI_MEMORY" else None,
                            facts={"return_1h":row.get("return_1h"),"residual_1h":row.get("residual_1h"),
                                   "oi_quantity_change":row.get("oi_quantity_change")},
                            inference="Conditional rebound is a hypothesis, not observed liquidation",
                            assumption="Fixed prior probabilities and cost model; no demonstrated edge"))
    return out

def register(store,config,packet,decisions):
    hour=packet["hour"];existing=store.get("registration",hour,optional=True)
    if existing:return existing
    if len(decisions)!=len(config["universe"])*3 or len({r["context_hash"] for r in decisions})!=1:
        raise ValueError("common_context_or_coverage")
    if {(r["policy"],r["symbol"]) for r in decisions}!={(p,s) for p in POLICIES for s in config["universe"]}:
        raise ValueError("policy_universe_mismatch")
    registered=utc();start=(instant(registered).replace(second=0,microsecond=0)+timedelta(minutes=1))
    if instant(packet["cutoff"])>instant(registered):raise ValueError("future_cutoff")
    ids=[];questions=[]
    with store.transaction():
        for symbol in config["universe"]:
            row=packet["assets"][symbol]
            for h in config["horizons_hours"]:
                qid=digest([hour,symbol,h]);questions.append(qid)
                q=dict(id=qid,symbol=symbol,horizon_hours=h,cutoff=packet["cutoff"],registered_at=registered,
                       deadline=(start+timedelta(hours=h)).isoformat(),universe=config["universe"],
                       origin_prices={s:r.get("book",{}).get("mid") for s,r in packet["assets"].items()},
                       assumed_cost_per_side=config["signal_markout_cost_per_side"],
                       stress_cost_per_side=config["stress_cost_per_side"],
                       target="asset_long_net_signal_markout_minus_equal_weight_others",
                       status="PENDING",execution_status="NOT_A_FILL")
                store.append("question",qid,q)
        for d in decisions:
            key=digest([hour,d["policy"],d["symbol"]]);ids.append(key)
            store.append("policy_decision",key,dict(d,id=key,hour=hour,registered_at=registered,
                         question_ids=[digest([hour,d["symbol"],h]) for h in config["horizons_hours"]]))
            if d["action"]=="LONG":
                store.append("paper_intent",key,dict(id=key,decision_id=key,policy=d["policy"],
                     symbol=d["symbol"],registered_at=registered,
                     earliest_entry=(instant(registered)+timedelta(seconds=config["entry_delay_seconds"])).isoformat(),
                     expires_at=(instant(registered)+timedelta(seconds=config["entry_expiry_seconds"])).isoformat(),
                     instrument="SPOT_LONG",status="PROPOSED_NOT_FILLED"))
        return store.append("registration",hour,dict(id=hour,registered_at=registered,
                     decision_ids=ids,question_ids=questions,context_hash=decisions[0]["context_hash"]))
