"""Offline-only, prefix-causal L1 features. No forecasts, providers or execution."""
import copy
import math
from .engine import Frame, instant, sha256, ReplayError

WINDOWS = (30, 60, 300)
MAX_GAP = 2.5

def build(frames, cutoff, protocol):
    cutoff = instant(cutoff)
    prefix = [copy.deepcopy(f) for f in frames if instant(f["received_at"]) <= cutoff]
    if not prefix:
        raise ReplayError("NO_AVAILABLE_HISTORY")
    parsed = [Frame.parse(f) for f in prefix]
    if len({f.id for f in parsed}) != len(parsed) or any(a.received_at >= b.received_at for a,b in zip(parsed,parsed[1:])):
        raise ReplayError("NON_MONOTONIC_HISTORY")
    symbols = set(parsed[0].quotes)
    if any(set(f.quotes)!=symbols for f in parsed):
        raise ReplayError("CHANGED_UNIVERSE")
    last = parsed[-1]
    if (cutoff-last.received_at).total_seconds() > MAX_GAP:
        raise ReplayError("STALE_FEATURE_CUTOFF")
    def window(seconds):
        target = cutoff.timestamp()-seconds
        anchors = [i for i,f in enumerate(parsed) if f.received_at.timestamp() <= target]
        if not anchors:
            return None, "INSUFFICIENT_HISTORY"
        i = anchors[-1]; selected = parsed[i:]
        if target-parsed[i].received_at.timestamp()>MAX_GAP or any((b.received_at-a.received_at).total_seconds()>MAX_GAP for a,b in zip(selected,selected[1:])):
            return None, "GAP"
        return selected, "AVAILABLE"
    result = dict(version="l1-prefix-v1",cutoff=cutoff.isoformat(), last_frame_id=last.id,
                  prefix_sha256=sha256(prefix),available_seconds=(last.received_at-parsed[0].received_at).total_seconds(),
                  windows_seconds=list(WINDOWS),max_gap_seconds=MAX_GAP,
                  long_horizons={k:"NOT_AVAILABLE_IN_THIS_CAPTURE" for k in ("1h","4h","1d")},symbols={})
    for symbol in sorted(symbols):
        q=last.quotes[symbol]
        cost=2*(protocol["fee_bps_per_side"]+protocol["slippage_bps_per_side"])+q.spread_bps
        row=dict(bid=q.bid,ask=q.ask,bid_size=q.bid_size,ask_size=q.ask_size,
                 imbalance=(q.bid_size-q.ask_size)/(q.bid_size+q.ask_size),spread_bps=q.spread_bps,
                 quote_age_seconds=(cutoff-q.exchange_at).total_seconds(),
                 frame_age_seconds=(cutoff-last.received_at).total_seconds(),
                 round_trip_cost_bps=cost,windows={}, expected_net_return=None,
                 expectation_status="NO_CALIBRATED_PROBABILITY_OR_CAUSAL_EVENT")
        for seconds in WINDOWS:
            selected,status=window(seconds)
            if selected is None:
                row["windows"][str(seconds)]={"status":status};continue
            if any((f.received_at-f.quotes[symbol].exchange_at).total_seconds()>protocol["max_quote_age_seconds"] for f in selected):
                row["windows"][str(seconds)]={"status":"STALE_QUOTE"};continue
            mids=[f.quotes[symbol].mid for f in selected]
            returns=[math.log(b/a) for a,b in zip(mids,mids[1:])]
            lo,hi=min(mids),max(mids); change=mids[-1]/mids[0]-1
            spreads=[f.quotes[symbol].spread_bps for f in selected]
            row["windows"][str(seconds)]=dict(status="AVAILABLE",samples=len(selected),
                from_at=selected[0].received_at.isoformat(),to_at=selected[-1].received_at.isoformat(),
                return_fraction=change,trend="UP" if change>0 else "DOWN" if change<0 else "FLAT",
                realized_vol=math.sqrt(sum(x*x for x in returns)),high=hi,low=lo,
                range_fraction=hi/lo-1,price_position=(mids[-1]-lo)/(hi-lo) if hi>lo else None,
                mean_spread_bps=sum(spreads)/len(spreads),spread_change_bps=spreads[-1]-spreads[0])
        sixty=row["windows"]["60"]
        row["volatility_state"]="UNAVAILABLE"
        if sixty["status"]=="AVAILABLE":
            selected,_=window(60); split=cutoff.timestamp()-30
            chunks=[[f.quotes[symbol].mid for f in selected if (f.received_at.timestamp()<=split)==old] for old in (True,False)]
            vols=[math.sqrt(sum(math.log(b/a)**2 for a,b in zip(m,m[1:]))) for m in chunks]
            row["volatility_state"]="EXPANDING" if vols[1]>vols[0] else "CONTRACTING" if vols[1]<vols[0] else "UNCHANGED"
            row["volatility_30s_halves"]=vols
        five=row["windows"]["300"]
        row["long_structure_candidate"]={"status":"UNAVAILABLE"}
        if five["status"]=="AVAILABLE":
            reward=five["high"]/q.mid-1-cost/10000
            risk=1-five["low"]/q.mid+cost/10000
            row["long_structure_candidate"]=dict(status="DESCRIPTIVE_NOT_FORECAST",stop_reference=five["low"],target_reference=five["high"],
                reward_after_cost=reward,risk_including_cost=risk,net_reward_risk=reward/risk,
                meets_2_to_1=reward>=2*risk,break_even_probability=risk/(risk+reward) if reward>0 else None)
        result["symbols"][symbol]=row
    return result

def enrich(context, frames):
    result=copy.deepcopy(context)
    features=build(frames,context["cutoff"],context["protocol"])
    if features["last_frame_id"]!=context["frame_id"]:
        raise ReplayError("FEATURE_CONTEXT_IDENTITY")
    for s,h in context["history"].items():
        q=features["symbols"][s]
        if h["current_bid"]!=q["bid"] or h["current_ask"]!=q["ask"]:
            raise ReplayError("FEATURE_CONTEXT_PRICE")
    result["market_features"]=features
    return result
