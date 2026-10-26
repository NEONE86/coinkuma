"""Independent stdlib-only calculation from captured RPC bytes; no ONE imports.

Usage: python recompute_chain_cycle.py RUNTIME SPEC_JSON CYCLE_JSON
Checks the supplied computation against raw blocks/receipts, preserves missing
identity/state as null, and never interprets a short sample as profitability.
"""
import argparse
from collections import Counter
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3

FEE = "0xdb249cf7a102711dda1beaba415bb011ee0e1402c7f67617236fed5739235d6c"
SWAP = "0x6150bb53cd76c5702d6074239fc7b6c2a5495d339eb122fdc06252f3efdbbc1b"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


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
    m = cycle["measurement"]
    assert sha(canonical(spec)) == m["spec_hash"]
    missing, raw_count = [], set()
    def get(method, params):
        ref = m["requests"].get(sha(canonical([method, params])))
        if ref is None:
            missing.append(dict(method=method, params=params, reason="NOT_CAPTURED"));return None
        r = records["retrieval_receipt", ref["receipt_id"]]
        assert r["request_payload"]["method"] == method and r["request_payload"]["params"] == params
        assert r["url"] == spec["endpoint"] and r["requested_at"] <= r["received_at"]
        raw = (runtime / "evidence" / r["path"]).read_bytes() if r.get("path") else None
        if raw is not None:
            assert sha(raw) == r["sha256"];raw_count.add(r["sha256"])
        if r.get("error"):
            assert r["snapshot_id"] is None
            missing.append(dict(method=method, params=params, reason=r["error"], response_sha256=r.get("sha256")));return None
        s = records["source_snapshot", r["snapshot_id"]]
        assert s["sha256"] == r["sha256"]
        d = json.loads(raw);assert d["id"] == r["request_payload"]["id"] and "error" not in d
        return d["result"]
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
    fee_rows, unresolved, totals, shared = [], [], Counter(), []
    pages, transactions, failed, logs, swaps, last = 0, 0, 0, 0, 0, None
    for height in range(spec["start_block"], spec["end_block"]+1):
        block = get("eth_getBlockByNumber", [hex(height), False])
        receipts = get("eth_getBlockReceipts", [hex(height)])
        if block is None or receipts is None: continue
        assert int(block["number"],16)==height
        if height==spec["start_block"]:assert block["hash"]==spec["start_hash"]
        if height==spec["end_block"]:assert block["hash"]==spec["end_hash"]
        if last is not None:assert block["parentHash"]==last
        last=block["hash"]
        assert len(receipts)==len(block["transactions"])
        assert {r["transactionHash"] for r in receipts}==set(block["transactions"])
        assert {int(r["transactionIndex"],16) for r in receipts}==set(range(len(receipts)))
        all_logs=[]
        for r in receipts:
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
            transfers=[l for l in r["logs"] if len(l["topics"])==3 and l["topics"][0]==TRANSFER]
            used=set()
            for l in r["logs"]:
                ts=l["topics"]
                if l["address"].lower()!=a["hook"] or not ts or ts[0]!=FEE:continue
                assert len(ts)==3 and len(l["data"])==66
                token=addr_topic(ts[2]);amount=int(l["data"],16)
                matches=[t for t in transfers if t["address"].lower()==token and addr_topic(t["topics"][1])==a["pool_manager"]
                    and addr_topic(t["topics"][2])==a["jar"] and int(t["data"],16)==amount and int(t["logIndex"],16) not in used]
                if not hook_swaps or addr_topic(ts[1])!=a["jar"] or token=="0x"+"0"*40 or not matches:
                    unresolved.append([r["transactionHash"],int(l["logIndex"],16)]);continue
                t=min(matches,key=lambda x:int(x["logIndex"],16));used.add(int(t["logIndex"],16))
                fee_rows.append((r["transactionHash"],int(l["logIndex"],16),token,str(amount),int(t["logIndex"],16)))
                totals[token]+=amount
            shared.extend(t for t in transfers if t["address"].lower()==a["uni"] and addr_topic(t["topics"][2])==spec["uni_sink"])
        assert sorted(all_logs)==list(range(len(all_logs)))
        pages+=1;transactions+=len(receipts);logs+=len(all_logs)
    recheck=get("eth_getBlockByNumber",[hex(spec["end_block"]),True])
    canonical_ok=recheck is not None and recheck["hash"]==spec["end_hash"]
    quantities=[];units_ok=True
    for token in sorted(set(totals)|{a["uni"]}):
        word=get("eth_call",[dict(to=token,data="0x313ce567"),hex(spec["end_block"])])
        decimals=int(word,16) if word is not None else None
        units_ok=units_ok and decimals is not None and 0<=decimals<=36 and (token!=a["uni"] or decimals==18)
        if token in totals:
            with localcontext() as ctx:
                ctx.prec=120
                quantity=format(Decimal(totals[token])/(Decimal(10)**decimals),"f") if decimals is not None else None
            quantities.append(dict(token=token,raw_amount=str(totals[token]),decimals=decimals,units=quantity))
    complete=bool(identity and units_ok and canonical_ok and pages==spec["end_block"]-spec["start_block"]+1)
    expected_rows=[(f["transaction_hash"],f["log_index"],f["token"],f["raw_amount"],f["transfer_log_index"]) for f in m["flows"]]
    assert fee_rows==expected_rows and quantities==m["totals"]
    assert len(shared)==len(m["shared_uni_sink_transfers"]) and swaps==len(m["hook_swaps"])
    assert ("COMPLETE" if complete else "DATA_MISSING")==m["status"]
    assert (len(fee_rows) if complete and not unresolved else None)==m["qualified_fee_count"]
    assert (complete and not fee_rows and not unresolved)==m["zero_proven"]
    assert m["causal_increment"]["value"] is None and m["hook_attributed_uni"] is None
    return dict(verification="PASS",status=m["status"],complete_pages=pages,transactions=transactions,
        failed_transactions=failed,logs=logs,hook_swap_logs=swaps,observed_reconciled_fee_logs=len(fee_rows),
        qualified_fee_count=m["qualified_fee_count"],zero_proven=m["zero_proven"],per_token_gross=quantities,
        canonical_uni_shared_sink_transfers=len(shared),causal_increment=None,missing=missing,
        checked_response_hashes=len(raw_count),production_imports=False)


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("runtime",type=Path);p.add_argument("spec",type=Path);p.add_argument("cycle",type=Path)
    args=p.parse_args()
    print(json.dumps(recompute(args.runtime,json.loads(args.spec.read_bytes()),json.loads(args.cycle.read_bytes())),indent=2))
