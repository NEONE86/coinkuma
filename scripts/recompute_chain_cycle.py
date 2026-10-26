"""Independent stdlib-only calculation from captured RPC bytes; no ONE imports.

Usage: python recompute_chain_cycle.py RUNTIME SPEC_JSON CYCLE_JSON
Checks the supplied computation against raw blocks/receipts, preserves missing
identity/state as null, and never interprets a short sample as profitability.
"""
import argparse
from collections import Counter
from decimal import Decimal, localcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

FEE = "0xdb249cf7a102711dda1beaba415bb011ee0e1402c7f67617236fed5739235d6c"
SWAP = "0x6150bb53cd76c5702d6074239fc7b6c2a5495d339eb122fdc06252f3efdbbc1b"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RELEASED = "0x0143172ff1dd87f3691e870b3fb5616db820278d26d2d16c3e03330a240a6c38"
VERSION = "chain-qualification-v2"


def canonical(x): return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
def sha(x): return hashlib.sha256(x).hexdigest()
def addr_topic(x):
    assert len(x) == 66 and x.startswith("0x" + "0" * 24)
    return "0x" + x[-40:].lower()


def recompute(runtime, spec, cycle):
    db = sqlite3.connect((runtime.resolve() / "research.sqlite3").as_uri() + "?mode=ro", uri=True)
    records = {}
    for kind, key, body, expected in db.execute("select kind,id,body,sha256 from records"):
        assert sha(body.encode()) == expected
        records[kind, key] = json.loads(body)
    db.close()
    assert records["collection_cycle", cycle["cycle_id"]] == cycle, "CYCLE_JSON differs from ledger"
    assert records["collection_spec", spec["spec_id"]] == spec, "SPEC_JSON differs from ledger"
    m = cycle["measurement"]
    version = m.get("measurement_version")
    assert version in (None, VERSION)
    assert sha(canonical(spec)) == m["spec_hash"]
    missing, raw_count, references = [], set(), {}
    def get(method, params):
        ref = m["requests"].get(sha(canonical([method, params])))
        if ref is None:
            missing.append(dict(method=method, params=params, reason="NOT_CAPTURED"));return None
        r = records["retrieval_receipt", ref["receipt_id"]]
        references[sha(canonical([method, params]))] = r
        assert r["request_payload"]["method"] == method and r["request_payload"]["params"] == params
        assert r["url"] == spec["endpoint"] and r["requested_at"] <= r["received_at"]
        assert r["request_sha256"] == sha(canonical(r["request_payload"]))
        assert datetime.fromisoformat(r["requested_at"]) >= datetime.fromisoformat(spec["frozen_at"])
        if r.get("path"):
            assert (runtime / "evidence" / r["path"]).resolve().parent == (runtime / "evidence/raw").resolve()
        raw = (runtime / "evidence" / r["path"]).read_bytes() if r.get("path") else None
        if raw is not None:
            assert sha(raw) == r["sha256"];raw_count.add(r["sha256"])
        if r.get("error"):
            assert r["snapshot_id"] is None
            missing.append(dict(method=method, params=params, reason=r["error"], response_sha256=r.get("sha256")));return None
        s = records["source_snapshot", r["snapshot_id"]]
        assert ref["snapshot_id"] == r["snapshot_id"] == "source-" + sha(canonical([r["url"], r["sha256"]]))
        assert s["sha256"] == r["sha256"] and s["raw_path"] == r["path"] and s["source_url"] == r["url"]
        d = json.loads(raw);assert d["id"] == r["request_payload"]["id"] and "error" not in d
        return d["result"]
    # Validate unused/error map entries too; the map cannot redirect an edge to
    # another request merely by changing a derived source ID.
    for key, ref in m["requests"].items():
        r = records["retrieval_receipt", ref["receipt_id"]]
        payload = r["request_payload"]
        assert key == sha(canonical([payload["method"], payload["params"]]))
        get(payload["method"], payload["params"])
    def sid(method, params): return references[sha(canonical([method, params]))]["snapshot_id"]
    chain = get("eth_chainId", [])
    finalized = get("eth_getBlockByNumber", ["finalized", False])
    identity = chain is not None and int(chain, 16) == 1 and finalized is not None and int(finalized["number"], 16) >= spec["end_block"]
    for role, proof in spec["identity_proofs"].items():
        code = get("eth_getCode", [proof["address"], hex(spec["end_block"])])
        identity = identity and code is not None and sha(bytes.fromhex(code[2:])) == proof["deployed_code_sha256"]
    a = spec["addresses"]
    for role, selector, target in (("hook", "490c98f5", a["jar"]), ("hook", "dc4c90d3", a["pool_manager"]),
            ("adapter", "e0ef2b4b", a["jar"]), ("jar", "02691bcb", a["firepit"]),
            ("firepit", "2b3ed84e", a["uni"]), ("firepit", "ec0fa86d", spec["uni_sink"])):
        word = get("eth_call", [dict(to=a[role], data="0x"+selector), hex(spec["end_block"])])
        identity = identity and word is not None and addr_topic(word) == target
    fee_rows, unresolved, totals, shared, swap_rows, headers, details = [], [], Counter(), [], [], {}, []
    pages, transactions, failed, logs, swaps, last = 0, 0, 0, 0, 0, None
    page_rows=[]
    for height in range(spec["start_block"], spec["end_block"]+1):
        block = get("eth_getBlockByNumber", [hex(height), False])
        headers[height] = block
        receipts = get("eth_getBlockReceipts", [hex(height)])
        if block is None: continue
        assert int(block["number"],16)==height
        if height==spec["start_block"]:assert block["hash"]==spec["start_hash"]
        if height==spec["end_block"]:assert block["hash"]==spec["end_hash"]
        if height in (spec["start_block"],spec["end_block"]):
            boundary="start_utc" if height==spec["start_block"] else "end_utc"
            assert int(block["timestamp"],16)==int(datetime.fromisoformat(spec[boundary]).timestamp())
        if last is not None:assert block["parentHash"]==last
        last=block["hash"]
        if receipts is None:continue
        assert len(receipts)==len(block["transactions"])
        assert {r["transactionHash"] for r in receipts}==set(block["transactions"])
        assert {int(r["transactionIndex"],16) for r in receipts}==set(range(len(receipts)))
        all_logs=[]
        rsid, bsid = sid("eth_getBlockReceipts", [hex(height)]), sid("eth_getBlockByNumber", [hex(height), False])
        effective = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc).isoformat()
        for ri, r in enumerate(receipts):
            assert r["transactionHash"]==block["transactions"][int(r["transactionIndex"],16)]
            assert r["blockHash"]==block["hash"] and r["blockNumber"]==block["number"]
            assert r["status"] in ("0x0","0x1")
            if r["status"]=="0x0":
                failed+=1;assert not r["logs"]
            for l in r["logs"]:
                assert l["removed"] is False and l["blockHash"]==block["hash"] and l["blockNumber"]==block["number"]
                assert l["transactionHash"]==r["transactionHash"] and l["transactionIndex"]==r["transactionIndex"]
                all_logs.append(int(l["logIndex"],16))
            hook_swaps=[l for l in r["logs"] if l["address"].lower()==a["hook"] and l["topics"] and l["topics"][0]==SWAP]
            swaps+=len(hook_swaps)
            swap_rows.extend(dict(transaction_hash=r["transactionHash"],block_hash=block["hash"],log_index=int(l["logIndex"],16),
                receipt_snapshot_id=rsid,block_snapshot_id=bsid) for l in hook_swaps)
            transfers=[l for l in r["logs"] if len(l["topics"])==3 and l["topics"][0]==TRANSFER]
            used=set()
            for li, l in enumerate(r["logs"]):
                ts=l["topics"]
                if l in hook_swaps or (l["address"].lower()==a["hook"] and ts and ts[0]==FEE) or (
                        l["address"].lower()==a["uni"] and len(ts)==3 and ts[0]==TRANSFER and addr_topic(ts[2])==spec["uni_sink"]):
                    details.append(dict(chain_id=spec["chain_id"],block_number=height,block_hash=block["hash"],
                        transaction_hash=r["transactionHash"],log_index=int(l["logIndex"],16),effective_at=effective,
                        json_pointer=f"/result/{ri}/logs/{li}",receipt_snapshot_id=rsid,block_snapshot_id=bsid,
                        emitter=l["address"],topics=ts,data=l["data"]))
                if l["address"].lower()!=a["hook"] or not ts or ts[0]!=FEE:continue
                assert len(ts)==3 and len(l["data"])==66
                token=addr_topic(ts[2]);amount=int(l["data"],16)
                entry=dict(chain_id=1,block_number=height,block_hash=block["hash"],transaction_hash=r["transactionHash"],
                    log_index=int(l["logIndex"],16),token=token,raw_amount=str(amount),effective_at=effective,
                    json_pointer=f"/result/{ri}/logs/{li}",fee_recipient=addr_topic(ts[1]),receipt_snapshot_id=rsid,block_snapshot_id=bsid)
                matches=[t for t in transfers if t["address"].lower()==token and addr_topic(t["topics"][1])==a["pool_manager"]
                    and addr_topic(t["topics"][2])==a["jar"] and int(t["data"],16)==amount and int(t["logIndex"],16) not in used]
                if not hook_swaps or addr_topic(ts[1])!=a["jar"] or token=="0x"+"0"*40 or not matches:
                    reason="no_swap_wrong_recipient_or_native_trace_required" if not hook_swaps or addr_topic(ts[1])!=a["jar"] or token=="0x"+"0"*40 else "erc20_transfer_not_reconciled"
                    unresolved.append(dict(entry,reason=reason));continue
                t=min(matches,key=lambda x:int(x["logIndex"],16));used.add(int(t["logIndex"],16))
                fee_rows.append(dict(entry,transfer_log_index=int(t["logIndex"],16),attribution="DIRECT_HOOK_TO_SHARED_JAR"))
                details[-1]["matching_transfer_json_pointer"]=f"/result/{ri}/logs/{r['logs'].index(t)}"
                totals[token]+=amount
            releases=[dict(log_index=int(l["logIndex"],16),json_pointer=f"/result/{ri}/logs/{li}",topics=l["topics"],data=l["data"])
                for li,l in enumerate(r["logs"]) if l["address"].lower()==a["firepit"] and len(l["topics"])==3 and l["topics"][0]==RELEASED]
            for t in transfers:
                if t["address"].lower()!=a["uni"] or addr_topic(t["topics"][2])!=spec["uni_sink"]:continue
                shared.append(dict(transaction_hash=r["transactionHash"],block_hash=block["hash"],log_index=int(t["logIndex"],16),
                    raw_amount=str(int(t["data"],16)),firepit_release_log_present=bool(releases),
                    attribution="UNATTRIBUTED_SHARED_JAR_CONTEXT",hook_attributed_amount=None,receipt_snapshot_id=rsid,block_snapshot_id=bsid))
                for d in details:
                    if d["transaction_hash"]==r["transactionHash"] and d["log_index"]==int(t["logIndex"],16):d["firepit_release_evidence"]=releases
        assert sorted(all_logs)==list(range(len(all_logs)))
        page_rows.append(dict(height=height,hash=block["hash"],parent_hash=block["parentHash"],transactions=len(receipts),
            failed_transactions=sum(r["status"]=="0x0" for r in receipts),receipt_snapshot_id=rsid,block_snapshot_id=bsid,status="COMPLETE"))
        pages+=1;transactions+=len(receipts);logs+=len(all_logs)
    recheck=get("eth_getBlockByNumber",[hex(spec["end_block"]),True])
    canonical_ok=recheck is not None and recheck["hash"]==spec["end_hash"]
    quantities=[];units_ok=True;metadata={}
    for token in sorted(set(totals)|{a["uni"]}):
        word=get("eth_call",[dict(to=token,data="0x313ce567"),hex(spec["end_block"])])
        decimals=int(word,16) if word is not None else None
        if decimals is not None and 0<=decimals<=36 and (token!=a["uni"] or decimals==18):
            metadata[token]=dict(decimals=decimals,evidence_snapshot_id=sid("eth_call",[dict(to=token,data="0x313ce567"),hex(spec["end_block"])]))
        units_ok=units_ok and decimals is not None and 0<=decimals<=36 and (token!=a["uni"] or decimals==18)
        if token in totals:
            with localcontext() as ctx:
                ctx.prec=120
                quantity=format(Decimal(totals[token])/(Decimal(10)**decimals),"f") if decimals is not None else None
            quantities.append(dict(token=token,raw_amount=str(totals[token]),decimals=decimals,units=quantity))
    complete=bool(identity and units_ok and canonical_ok and pages==spec["end_block"]-spec["start_block"]+1)
    assert m["identity_verified"] is bool(identity)
    assert m["pages"]==page_rows, "page coverage/provenance mismatch"
    qualified=[];fact_results=[];qualified_totals=Counter()
    for row in fee_rows:
        prior=row["block_hash"];path_ok=canonical_ok
        for h in range(row["block_number"],spec["end_block"]+1):
            b=headers[h]
            if b is None or (b["hash"]!=prior if h==row["block_number"] else b["parentHash"]!=prior):
                path_ok=False;break
            prior=b["hash"]
        checks=dict(identity_and_route=bool(identity),finalized=finalized is not None and int(finalized["number"],16)>=spec["end_block"],
            canonical_membership=bool(path_ok and prior==spec["end_hash"]),receipt_and_transfer=True,units=row["token"] in metadata)
        fid=":".join(map(str,(spec["chain_id"],row["block_hash"],row["transaction_hash"],row["log_index"])))
        eligible=all(v is True for v in checks.values())
        fact_results.append(dict(fact_id=fid,status="QUALIFIED" if eligible else "UNVERIFIED",checks=checks,
            failed_checks=sorted(k for k,v in checks.items() if v is not True),state_validation_block=spec["end_block"]))
        if eligible:qualified.append(fid);qualified_totals[row["token"]]+=int(row["raw_amount"])
        if version:
            row["qualification_status"]="QUALIFIED" if eligible else "UNVERIFIED"
            if not eligible:row["attribution"]="UNVERIFIED_CANDIDATE"
    verified_complete=complete and len(qualified)==len(fee_rows) and not unresolved
    validation=dict(version=VERSION,facts=fact_results,qualified_fact_ids=qualified,range_complete=verified_complete,
        observed_qualified_fee_count=len(qualified),range_fee_count=len(qualified) if verified_complete else None,
        zero_proven=verified_complete and not qualified,event_status="EXECUTED" if qualified else "CONFIGURED"
            if identity and canonical_ok and a["uni"] in metadata else "PROPOSED")
    if version:
        with localcontext() as ctx:
            ctx.prec=120
            quantities=[dict(token=t,raw_amount=str(v),decimals=metadata[t]["decimals"],
                units=format(Decimal(v)/(Decimal(10)**metadata[t]["decimals"]),"f")) for t,v in sorted(qualified_totals.items())]
        assert m["validation"]==validation, "validation semantics differ"
        assert m["observed_qualified_fee_count"]==len(qualified)
        assert m["qualified_fee_count"]==validation["range_fee_count"]
        assert m["zero_proven"] is validation["zero_proven"]
        assert m["status"]==("COMPLETE" if verified_complete else "DATA_MISSING")
        assert m["totals_scope"]==("COMPLETE_RANGE" if verified_complete else "OBSERVED_QUALIFIED_LOWER_BOUND")
    else:
        assert ("COMPLETE" if complete else "DATA_MISSING")==m["status"]
        assert (len(fee_rows) if complete and not unresolved else None)==m["qualified_fee_count"]
        assert (complete and not fee_rows and not unresolved)==m["zero_proven"]
    assert fee_rows==m["flows"], "flow identity/quantity/time/provenance mismatch"
    assert shared==m["shared_uni_sink_transfers"], "shared UNI/Firepit mismatch"
    assert swap_rows==m["hook_swaps"], "swap identity/provenance mismatch"
    assert unresolved==m["unreconciled_fee_claims"]
    assert metadata==m["token_metadata"] and quantities==m["totals"]
    assert m["causal_increment"]["value"] is None and m["causal_increment"]["status"]=="AMBIGUOUS" and m["hook_attributed_uni"] is None
    return dict(verification="PASS",status=m["status"],complete_pages=pages,transactions=transactions,
        failed_transactions=failed,logs=logs,hook_swap_logs=swaps,observed_reconciled_fee_logs=len(fee_rows),
        qualified_fee_count=m["qualified_fee_count"],zero_proven=m["zero_proven"],per_token_gross=quantities,
        canonical_uni_shared_sink_transfers=len(shared),causal_increment=None,missing=missing,
        checked_response_hashes=len(raw_count),production_imports=False,cycle_matches_ledger=True,
        measurement_version=version or "legacy-v1",validation_version=VERSION,
        validation=validation,checked_semantic_details=details)


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("runtime",type=Path);p.add_argument("spec",type=Path);p.add_argument("cycle",type=Path)
    args=p.parse_args()
    print(json.dumps(recompute(args.runtime,json.loads(args.spec.read_bytes()),json.loads(args.cycle.read_bytes())),indent=2))
