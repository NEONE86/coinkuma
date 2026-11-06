"""CLI for ONE Zero Discovery. Standard library only, no network or model calls."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import reconcile
from .evolve import SearchConfig, search, splits
from .genome import Genome, canonical, digest
from .market import DataError, load_csv
from .replay import Limits, simulate


def create_demo(path:Path, *, rows:int=900, seed:int=123, world:str="noedge")->None:
    """Produce controlled synthetic worlds (never exchange data).

    noedge: an ordinary alternating-drift stress world.
    leadflow: the current bar's taker buy/sell imbalance CAUSES the *next*
    bar's sign. There is no current-bar future leak: subsequent price is
    generated from the previously emitted flow. This is a positive control.
    """
    if rows<400 or rows>50000:raise ValueError("DEMO_ROW_RANGE")
    if world not in ("noedge","leadflow"):raise ValueError("DEMO_WORLD")
    r=random.Random(seed)
    symbols=("BTCUSDT","ETHUSDT","SOLUSDT")
    p={"BTCUSDT":30000.,"ETHUSDT":1800.,"SOLUSDT":40.}
    prev_signal={s:r.choice((-1,1)) for s in symbols}
    t0=datetime(2023,1,1,tzinfo=timezone.utc)
    with path.open("x",encoding="utf-8",newline="") as f:
        w=csv.writer(f)
        w.writerow(["timestamp","symbol","open","high","low","close","volume","funding_rate","aggressive_buy","aggressive_sell"])
        for i in range(rows):
            t=t0+timedelta(hours=4*i)
            drift=([.002,-.001,.0015,-.002][(i//180)%4])
            marketshock=r.gauss(0,.012)
            for j,s in enumerate(symbols):
                op=p[s]
                signal=r.choice((-1,1))
                if world=="leadflow":
                    change=prev_signal[s]*.016+r.gauss(0,.001)
                else:
                    change=drift*(1+j*.3)+marketshock+r.gauss(0,.006+j*.004)
                cl=max(.01,op*math.exp(change))
                high=max(op,cl)*(1+r.random()*.007)
                low=min(op,cl)*(1-r.random()*.007)
                volume=1e6*(1+j)*math.exp(r.gauss(0,.6))
                funding=.00002 if i%2==0 else -.00001
                taker_bias=signal*.90 if world=="leadflow" else r.uniform(-.8,.8)
                buy=volume*(1+taker_bias)/2
                sell=volume*(1-taker_bias)/2
                w.writerow([t.isoformat(),s,f"{op:.8f}",f"{high:.8f}",f"{low:.8f}",f"{cl:.8f}",
                            f"{volume:.3f}",funding,f"{buy:.3f}",f"{sell:.3f}"])
                p[s]=cl
                prev_signal[s]=signal


def safe_write(path:Path,obj:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create and fsync: no silent evidence overwrite.
    data=json.dumps(obj,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2).encode("utf-8")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(data);f.flush();os.fsync(f.fileno())


def baseline_hold(features:tuple[str,...])->Genome:
    return Genome.from_dict({"type":"EXPRESSION","scores":{
        "LONG":1.,"SHORT":-2.,"FLAT":-2.,"HOLD":["mul",3.,["abs","position"]]
    },"size":3.},features)


def run(args:argparse.Namespace)->dict:
    market=load_csv(args.data,source_type=args.source_kind)
    limits=Limits(args.capital,args.fee_bps,args.slippage_bps,args.max_symbol,args.max_gross,args.halt_dd)
    config=SearchConfig(args.seed,args.population,args.generations)
    props=None
    if args.proposals:
        props=json.loads(Path(args.proposals).read_text(encoding="utf-8"))
        if not isinstance(props,list) or len(props)>128:raise ValueError("PROPOSAL_ARRAY_MAX_128")
    result=search(market,limits,config,proposals=props)
    hold=baseline_hold(market.active_features)
    holdout=simulate(market,hold,*splits(market)["holdout"],limits).summary()
    # An autonomous discovery must beat an executable non-AI control as well as CASH.
    if result["status"]=="RESEARCH_CHALLENGER_POSITIVE" and result["holdout"]["net_multiple"]<=holdout["net_multiple"]:
        result["status"]="NO_PROMOTABLE_EDGE"
    out={"schema":"ONE_ZERO_DISCOVERY_V1","research_only":True,
         "source":{"kind":market.source_type,"sha256":market.source_hash,"symbols":list(market.symbols),
                   "n_bars":len(market.times),"first":market.times[0].isoformat(),"last":market.times[-1].isoformat(),
                   "funding_known":market.funding_known,"active_observations":list(market.active_features)},
         "limits":asdict(limits),"search_config":asdict(config),"result":result,
         "holdout_long_only":holdout,
         "warning":"Synthetic results are NOT financial evidence. Historical search already used in iteration is NOT virgin OOS. This engine excludes liquidation, fill depth, latency and mark-price events; PAPER ONLY."}
    out["manifest_sha256"]=digest(out)
    safe_write(Path(args.output),out)
    return out


def verify(args:argparse.Namespace)->dict:
    raw=json.loads(Path(args.report).read_text(encoding="utf-8"))
    supplied=raw.pop("manifest_sha256",None)
    if supplied!=digest(raw):raise ValueError("REPORT_MANIFEST_CHANGED")
    market=load_csv(args.data,source_type=raw["source"]["kind"])
    if raw["source"]["sha256"]!=market.source_hash:raise ValueError("DATASET_HASH_MISMATCH")
    lim=Limits(**raw["limits"])
    champ=Genome.from_dict(raw["result"]["champion"],market.active_features)
    if champ.sha!=raw["result"]["champion_sha"]:raise ValueError("CHAMPION_HASH_MISMATCH")
    folds=splits(market)
    for key in ("train","validation","holdout"):
        again=simulate(market,champ,*folds[key],lim,capture=key=="holdout")
        if again.summary()!=raw["result"][key]:raise ValueError("REPLAY_MISMATCH:"+key)
        if key=="holdout":
            if again.events!=raw["result"]["holdout_events"]:raise ValueError("EVENT_SEQUENCE_MISMATCH")
            if again.equity_points!=raw["result"]["holdout_equity"]:raise ValueError("EQUITY_POINTS_MISMATCH")
    cash=Genome((0.,0.,0.,0.),0.,True)
    if simulate(market,cash,*folds["holdout"],lim).summary()!=raw["result"]["holdout_cash"]:
        raise ValueError("CASH_COMPARATOR_MISMATCH")
    if simulate(market,baseline_hold(market.active_features),*folds["holdout"],lim).summary()!=raw["holdout_long_only"]:
        raise ValueError("LONG_COMPARATOR_MISMATCH")
    if raw["result"]["holdout"].get("ruin"):
        audit={"status":"UNAVAILABLE_RUIN_PATH"}
    else:
        audit=reconcile(market,folds["holdout"],lim,
                        raw["result"]["holdout_events"],raw["result"]["holdout_equity"],raw["result"]["holdout"])
    return {"status":"PASS_DETERMINISTIC_REPLAY","economic_audit":audit,
            "source_hash":market.source_hash,"champion_hash":champ.sha}


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="ONE Zero: offline strategy discovery, no network/orders")
    subs=ap.add_subparsers(dest="cmd",required=True)
    d=subs.add_parser("demo",help="create clearly labeled SYNTHETIC market CSV")
    d.add_argument("--output",required=True);d.add_argument("--bars",type=int,default=900)
    d.add_argument("--world",choices=["noedge","leadflow"],default="noedge")
    p=subs.add_parser("run",help="evolve and evaluate on a supplied CSV")
    p.add_argument("--data",required=True);p.add_argument("--output",required=True)
    p.add_argument("--source-kind",required=True,choices=["SYNTHETIC_DEMO","EXTERNAL_HISTORICAL_UNVERIFIED","PROSPECTIVE_OFFLINE_REPLAY"])
    p.add_argument("--capital",type=float,default=10000.);p.add_argument("--fee-bps",type=float,default=8.);p.add_argument("--slippage-bps",type=float,default=7.)
    p.add_argument("--max-symbol",type=float,default=.75);p.add_argument("--max-gross",type=float,default=1.5);p.add_argument("--halt-dd",type=float,default=.55)
    p.add_argument("--seed",type=int,default=77);p.add_argument("--population",type=int,default=24);p.add_argument("--generations",type=int,default=6)
    p.add_argument("--proposals",default=None,help="optional JSON array of validated external AI proposals; no model is called")
    v=subs.add_parser("verify",help="verify unmodified source, replay and ledger arithmetic")
    v.add_argument("--data",required=True);v.add_argument("--report",required=True)
    a=ap.parse_args(argv)
    try:
        if a.cmd=="demo":
            create_demo(Path(a.output),rows=a.bars,world=a.world)
            print(json.dumps({"created":a.output,"data_use":"SYNTHETIC_DEMO","market_evidence":False}))
        elif a.cmd=="run":
            result=run(a)
            r=result["result"]
            print(json.dumps({"report":a.output,"source":result["source"]["kind"],"status":r["status"],
             "unique_candidates":r["search"]["evaluated_unique"],"champion_sha":r["champion_sha"],
             "train":r["train"]["net_multiple"],"validation":r["validation"]["net_multiple"],
             "holdout":r["holdout"]["net_multiple"],"cash":r["holdout_cash"]["net_multiple"],
             "long_control":result["holdout_long_only"]["net_multiple"]},ensure_ascii=False))
        elif a.cmd=="verify":print(json.dumps(verify(a),ensure_ascii=False))
    except (OSError,ValueError,DataError,KeyError,TypeError) as exc:
        print(json.dumps({"status":"FAILED_CLOSED","reason":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2
    return 0


if __name__=="__main__":raise SystemExit(main())
