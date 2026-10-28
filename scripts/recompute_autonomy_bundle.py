#!/usr/bin/env python3
"""Standalone stdlib audit of a private export; imports no strategy code."""
import argparse,hashlib,json,math,sqlite3
from pathlib import Path
from datetime import datetime,timezone

def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sha(x):return hashlib.sha256(x).hexdigest()
def stamp(x):
    d=datetime.fromisoformat(x.replace("Z","+00:00"))
    assert d.tzinfo is not None,"naive_timestamp"
    return d.astimezone(timezone.utc)
def close(a,b):assert math.isfinite(float(a)) and math.isfinite(float(b)) and math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7),(a,b)
def read_ledger(path):
    c=sqlite3.connect(path.resolve().as_uri()+"?mode=ro&immutable=1",uri=True)
    c.row_factory=sqlite3.Row
    assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    rows=[dict(r) for r in c.execute("SELECT * FROM records ORDER BY sequence")];c.close()
    for row in rows:
        assert sha(row["body"].encode())==row["sha256"],"ledger_hash"
        row["parsed"]=json.loads(row["body"])
        assert sha(canonical(row["parsed"]))==row["sha256"],"noncanonical_body"
    return rows

def audit(root):
    manifest_path=root/"MANIFEST.json"
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        for relative,expected in manifest["files"].items():
            path=(root/relative).resolve()
            assert root.resolve() in path.parents,"manifest_path_escape"
            assert sha(path.read_bytes())==expected,"manifest_hash:"+relative
    runtime=root/"runtime";rows=read_ledger(runtime/"research.sqlite3")
    kinds={}
    for r in rows:kinds.setdefault(r["kind"],{})[r["id"]]=r["parsed"]
    get=lambda kind:list(kinds.get(kind,{}).values())
    config=get("autonomy_config")[0]["config"]
    assert sum(config["engine_capital_krw"].values())<=config["total_capital_krw"]<=20000000
    assert config["live_trading"] is False and config["instrument"]=="SPOT_LONG"
    receipts=kinds.get("http_receipt",{})
    for receipt in list(receipts.values())+get("growth_source"):
        assert stamp(receipt["requested_at"])<=stamp(receipt["received_at"])
        assert sha((runtime/"raw"/(receipt["sha256"]+".bin")).read_bytes())==receipt["sha256"]
    def response(receipt_id):
        r=receipts[receipt_id];return json.loads((runtime/"raw"/(r["sha256"]+".bin")).read_bytes())
    packets=kinds.get("market_packet",{})
    for packet in packets.values():
        cutoff=stamp(packet["cutoff"]);returns={}
        for key in packet["receipts"]:
            assert stamp(receipts[key]["received_at"])<=cutoff
        for symbol,r in packet["assets"].items():
            assert r["symbol"]==symbol
            if r["price_status"]!="VALID":continue
            book=r["book"];depth=response(book["receipt_id"])
            mid=(float(depth["asks"][0][0])+float(depth["bids"][0][0]))/2
            close(book["mid"],mid)
            bar_receipt=next(k for k in r["receipts"] if "/klines?" in receipts[k]["url"])
            bars=response(bar_receipt)
            ret=float(bars[-1][4])/float(bars[-2][4])-1;close(ret,r["return_1h"]);returns[symbol]=ret
            if r["numeric_status"]=="VALID":
                oi_id=next(k for k in packet["receipts"] if "openInterestHist?" in receipts[k]["url"] and ("symbol="+symbol+"&") in receipts[k]["url"])
                oi=sorted(response(oi_id),key=lambda x:x["timestamp"])
                close(r["oi_quantity_change"],float(oi[-1]["sumOpenInterest"])/float(oi[-2]["sumOpenInterest"])-1)
                close(r["oi_notional_change"],float(oi[-1]["sumOpenInterestValue"])/float(oi[-2]["sumOpenInterestValue"])-1)
                f_id=next(k for k in packet["receipts"] if "/fundingRate?" in receipts[k]["url"] and ("symbol="+symbol+"&") in receipts[k]["url"])
                f=sorted(response(f_id),key=lambda x:x["fundingTime"])
                interval=(f[-1]["fundingTime"]-f[-2]["fundingTime"])/3600000
                close(r["observed_funding_interval_hours"],interval)
                close(r["last_settled_funding_per_hour"],float(f[-1]["fundingRate"])/interval)
                assert r["oi_quantity_unit"]=="base_asset" and r["oi_notional_unit"]=="USDT"
        if len(returns)==len(packet["universe"]):
            for symbol,ret in returns.items():
                other=[v for k,v in returns.items() if k!=symbol]
                close(packet["assets"][symbol]["residual_1h"],ret-sum(other)/len(other))
    questions=kinds.get("question",{});decisions=get("policy_decision")
    for registration in get("registration"):
        ds=[kinds["policy_decision"][k] for k in registration["decision_ids"]]
        assert len(ds)==len(config["universe"])*3
        assert len({d["context_hash"] for d in ds})==1
        assert {(d["policy"],d["symbol"]) for d in ds}=={(p,s) for p in config["policies"] for s in config["universe"]}
        packet=packets[registration["id"]]
        for d in ds:
            assert d["context_hash"]==sha(canonical(d["context"]))
            assert d["context"]["packet_hash"]==sha(canonical(packet))
            assert d["context"]["cutoff"]==packet["cutoff"]
            assert stamp(packet["cutoff"])<=stamp(d["registered_at"])<=stamp(d["stored_at"])
            for qid in d["question_ids"]:
                q=questions[qid];assert q["symbol"]==d["symbol"]
                delta=(stamp(q["deadline"])-stamp(q["registered_at"])).total_seconds()
                assert q["horizon_hours"]*3600<=delta<=q["horizon_hours"]*3600+60
                assert stamp(q["cutoff"])<=stamp(q["registered_at"])
                for s,price in q["origin_prices"].items():
                    if price is not None:close(price,packet["assets"][s]["book"]["mid"])
    for d in decisions:
        assert d["action"] in ("LONG","NO_TRADE")
        assert set(d["probabilities"])=={"6","24","72"}
        for value in d["probabilities"].values():
            assert value is None or (type(value) in (int,float) and 0<=value<=1)
        if d["policy"]=="CF_AI_MEMORY":
            call=kinds["model_call"][d["ai_call_id"]]
            if call["status"]=="CONNECTED_VALID":
                answer=next(a for a in call["answer"]["assets"] if a["symbol"]==d["symbol"])
                for h in (6,24,72):close(d["probabilities"][str(h)],answer["p_excess_"+str(h)+"h"])
            else:assert all(v is None for v in d["probabilities"].values()) and d["action"]=="NO_TRADE"
    for call in get("model_call"):
        folder=runtime/call.get("artifact_directory","models/"+call["id"])
        payload=json.loads((folder/"input.json").read_text(encoding="utf-8"))
        assert sha(canonical(payload))==call["input_hash"],"model_input_hash"
        assert sha((folder/"prompt.txt").read_bytes())==call["prompt_hash"],"model_prompt_hash"
        assert stamp(call["started_at"])<=stamp(call["completed_at"])
        if call["status"]=="CONNECTED_VALID":
            answer=json.loads((folder/"answer.json").read_text(encoding="utf-8"))
            assert answer==call["answer"] and sha(canonical(answer))==call["response_hash"]
            for m in payload.get("memory",[]):
                assert stamp(m["resolved_at"])<=stamp(payload["packet"]["cutoff"])
                if "available_at" in m:assert stamp(m["available_at"])<=stamp(payload["packet"]["cutoff"])
        for suffix,field in (("stdout.jsonl","stdout_hash"),("stderr.txt","stderr_hash")):
            if field in call:assert sha((folder/suffix).read_bytes())==call[field]
    scores={p:[] for p in config["policies"]}
    for outcome in get("outcome"):
        q=questions[outcome["question_id"]]
        assert outcome["status"]=="RESOLVED" and type(outcome["label"]) is bool
        assert stamp(outcome["resolved_at"])>=stamp(q["deadline"])
        returns={}
        for symbol,price in outcome["prices"].items():
            raw=response(price["receipt_id"]);bar=raw[0]
            assert int(stamp(q["deadline"]).timestamp()*1000)-1==bar[6]
            close(float(bar[4]),price["price"])
            returns[symbol]=price["price"]/q["origin_prices"][symbol]-1
        gross=returns[q["symbol"]];c=q["assumed_cost_per_side"]
        net=(1+gross)*(1-c)/(1+c)-1
        other=[v for s,v in returns.items() if s!=q["symbol"]];excess=net-sum(other)/len(other)
        close(net,outcome["net_signal_return"]);close(excess,outcome["excess_return"])
        assert outcome["label"] is (excess>0)
        for d in decisions:
            if q["id"] in d["question_ids"]:
                p=d["probabilities"][str(q["horizon_hours"])]
                if p is not None:scores[d["policy"]].append((p-int(excess>0))**2)
    entries=kinds.get("paper_entry",{});exits=kinds.get("paper_exit",{})
    cash={p:config["engine_capital_krw"]["CASHFLOW"] for p in config["policies"]}
    for key,e in entries.items():
        intent=kinds["paper_intent"][key]
        assert stamp(e["filled_at"])>=stamp(intent["earliest_entry"])
        assert stamp(receipts[e["book_receipt_id"]]["received_at"])>=stamp(intent["earliest_entry"])
        assert e["quantity"]>0 and e["funding_status"]=="NOT_APPLICABLE_SPOT" and e["funding_usdt"]==0
        close(e["cost_krw"],-e["cash_delta_krw"])
        close(e["cash_delta_krw"],e["cash_delta_usdt"]*config["fx_krw_per_usdt"])
        close(e["fee_usdt"],e["gross_usdt"]*config["fee_bps_per_side"]/10000)
        cash[e["policy"]]+=e["cash_delta_krw"]
    for key,x in exits.items():
        e=entries[key];assert x["policy"]==e["policy"] and x["symbol"]==e["symbol"]
        close(x["quantity"],e["quantity"]);close(x["realized_pnl_krw"],x["cash_delta_krw"]-e["cost_krw"])
        cash[x["policy"]]+=x["cash_delta_krw"]
    for g in get("growth_experiment"):
        assert len(g["causes"])==3 and len({c["mechanism"] for c in g["causes"]})==3
        assert len(g["controls"])==12 and len({c["context_hash"] for c in g["controls"]})==1
        assert all(stamp(t)<=stamp(g["cutoff"])<=stamp(g["registered_at"]) for t in g["first_observed_at"].values())
        assert (stamp(g["economic_deadline"])-stamp(g["registered_at"])).days>=90
        for cause in g["causes"]:
            cs=[c for c in g["controls"] if c["cause_id"]==cause["id"]]
            assert {c["policy"] for c in cs}=={"G_DIRECT","G_SECTOR","G_INDIRECT","G_LAGGED"}
    prior={}
    if (root/"prior"/"baseline-340.json").exists():
        baseline=json.loads((root/"prior"/"baseline-340.json").read_text(encoding="utf-8"))
        actual=read_ledger(root/"prior"/"preserved-340.sqlite3")
        envelopes=[{k:v for k,v in r.items() if k!="parsed"} for r in actual]
        # Baseline may store bodies as parsed JSON or original canonical text.
        for r in envelopes:
            if baseline and isinstance(baseline[0].get("body"),dict):r["body"]=json.loads(r["body"])
        assert envelopes==baseline,"prior_envelopes_changed"
        rawfiles=list((root/"prior"/"evidence"/"raw").glob("*.bin"))
        for p in rawfiles:assert sha(p.read_bytes())==p.stem,"prior_raw_changed"
        prior=dict(preserved_records=len(actual),verified_raw_files=len(rawfiles))
    return dict(ledger_records=len(rows),market_packets=len(packets),
        independent_hours=len({r["hour"] for r in get("run_complete")}),
        policy_decisions=len(decisions),forecast_slots=len(decisions)*3,
        numeric_probability_forecasts=sum(v is not None for d in decisions for v in d["probabilities"].values()),
        missing_model_forecasts=sum(v is None for d in decisions for v in d["probabilities"].values()),
        resolved_questions=len(get("outcome")),pending_questions=len(questions)-len(get("outcome")),
        model_statuses={s:sum(c["status"]==s for c in get("model_call")) for s in {c["status"] for c in get("model_call")}},
        brier={p:sum(v)/len(v) if v else None for p,v in scores.items()},
        cash_krw=cash,paper_entries=len(entries),paper_exits=len(exits),
        growth_causes=sum(len(g["causes"]) for g in get("growth_experiment")),
        growth_controls=len(get("growth_policy")),prior=prior,
        EDGE_PROVEN=False,LIVE_TRADING_ENABLED=False,
        provenance_limit="Local append-only receipts and hashes are not external timestamp notarization.")
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("bundle",type=Path);a=p.parse_args()
    print(json.dumps(audit(a.bundle),ensure_ascii=False,indent=2))
