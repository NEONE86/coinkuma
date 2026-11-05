"""Read-only historical diagnostics; injected plans are NOT AI performance.
python -m one_trading.ai_native.offline_input_study --run-dir ... --output-dir ...
"""
import argparse
import json
import time
from pathlib import Path
from .engine import Experiment, Protocol, ModelDecision, canonical, sha256
from .planners import plan, CashPlanner
from .audit import verify
from .live_audit import verify as verify_original
from .bybit_audit import audit
from .causal_features import enrich

class InjectedPlans:
    def __init__(self, frames):
        self.actions={frames[0]["id"]:"BUY",frames[20]["id"]:"SELL"}
    def propose(self, c):
        action=self.actions.get(c["frame_id"],"HOLD")
        return ModelDecision(plan(action,"BTCUSDT" if action!="HOLD" else "CASH",
            .0011 if action=="BUY" else 1. if action=="SELL" else 0.,
            "OFFLINE INJECTED FUNCTIONAL TEST NOT AI"),.5)

def study(root, output):
    root=Path(root).resolve();output=Path(output).resolve()
    repo=Path(__file__).resolve().parents[2]
    if output.exists() or output.is_relative_to(repo) or output.is_relative_to(root):
        raise ValueError("NEW_OUTPUT_OUTSIDE_REPO_AND_SOURCE_REQUIRED")
    source=audit(root/"source")
    original_audit=verify_original(root)
    frames=[json.loads(x) for x in (root/"source/frames.jsonl").read_bytes().splitlines()]
    raw={x["seq"]:x for x in (json.loads(l) for l in (root/"source/raw.jsonl").read_bytes().splitlines())}
    provenance={x["frame_id"]:x for x in (json.loads(l) for l in (root/"source/events.jsonl").read_bytes().splitlines()) if x["kind"]=="frame_provenance"}
    output.mkdir()
    examples=[];start=time.perf_counter()
    for f in sorted(root.glob("request-*-input.json")):
        c=json.loads(f.read_text());e=enrich(c,frames)
        (output/f.name).write_bytes(canonical(e));examples.append(dict(request=f.name,original_sha256=sha256(c),enhanced_sha256=sha256(e),available_seconds=e["market_features"]["available_seconds"]))
    feature_seconds=time.perf_counter()-start
    e=Experiment(Protocol(),{"CASH":CashPlanner(),"INJECTED_NOT_AI":InjectedPlans(frames)},data_use="REUSED_HISTORICAL")
    for f in frames:
        refs=provenance[f["id"]]["source_seqs"]
        e.observe(f,source_received_at={s:raw[n]["received_at"] for s,n in refs.items()})
    paper=e.finish();verified=verify(paper)
    (output/"injected-paper.json").write_bytes(canonical(paper))
    report=dict(classification="ACTUAL_RECORDED_QUOTES_WITH_INJECTED_PLANS_NOT_AI",source_audit=source,
        original_input_audit=original_audit["status"],examples=examples,feature_compute_seconds=feature_seconds,external_calls=0,model_cost_usdt=0,
        paper_audit=verified,summary=paper["summary"],risk_rejections=[r["payload"] for r in paper["ledger"] if r["kind"]=="order_rejected"],
        formulas_version="l1-prefix-v1",live_adapter_enabled=False)
    (output/"report.json").write_bytes(canonical(report))
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--run-dir",required=True);p.add_argument("--output-dir",required=True)
    a=p.parse_args();print(json.dumps(study(a.run_dir,a.output_dir)))

