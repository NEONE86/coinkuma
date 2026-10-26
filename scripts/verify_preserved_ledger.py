"""Independent stdlib preservation/chronology check for the private bundle."""
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime
from verify_collector_bundle import verify
from recompute_chain_cycle import canonical, sha


def check(root):
    root = Path(root)
    baseline = json.loads((root / "baseline-envelopes.json").read_bytes())
    all_rows = [json.loads(line) for line in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    db = sqlite3.connect((root.resolve() / "runtime/research.sqlite3").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert all_rows == [dict(r) for r in db.execute("select * from records order by sequence")]
    db.close()
    assert len(baseline) == 179 and all_rows[:179] == baseline
    records = {(r["kind"], r["id"]): json.loads(r["body"]) for r in all_rows}
    for row in all_rows: assert sha(row["body"].encode()) == row["sha256"]
    raw_result = verify(root / "runtime", json.loads((root / "baseline-raw-sha256.json").read_bytes()))
    for row in all_rows:
        if row["kind"] == "source_snapshot":
            s = json.loads(row["body"])
            assert sha((root / "runtime/evidence" / s["raw_path"]).read_bytes()) == s["sha256"]
        if row["kind"] == "retrieval_receipt":
            r = json.loads(row["body"])
            assert datetime.fromisoformat(r["requested_at"]) <= datetime.fromisoformat(r["received_at"])
            if r.get("path"): assert sha((root / "runtime/evidence" / r["path"]).read_bytes()) == r["sha256"]
    forecasts = [b for (kind, _), b in records.items() if kind == "forecast"]
    assert len(forecasts) == 12
    assert not [b for (kind, _), b in records.items() if kind in {"resolution", "paper_fill"}]
    packet_count = 0
    for (kind, _), p in records.items():
        if kind != "evidence_packet": continue
        refs = []
        for k, field in (("asset_identity", "subject_asset_ids"), ("economic_event_revision", "event_revision_ids"),
                ("economic_flow", "economic_flow_ids"), ("observation", "observation_ids"),
                ("source_snapshot", "source_snapshot_ids"), ("price_snapshot", "price_snapshot_ids")):
            for rid in p[field]:
                b = records[k, rid]
                assert datetime.fromisoformat(b.get("available_at", b.get("registered_at"))) <= datetime.fromisoformat(p["as_of"])
                refs.append([k, rid, sha(canonical(b))])
        header = {k: v for k, v in p.items() if k not in {"packet_id", "manifest_hash"}}
        assert sha(canonical(dict(packet=header, input_hashes=refs))) == p["manifest_hash"]
        packet_count += 1
    return dict(status="PASS",original_envelopes_unchanged=179,current_envelopes=len(all_rows),
        old_raw_unchanged=raw_result["files"],packet_hashes_and_cutoffs_checked=packet_count,
        forecasts=12,resolutions=0,production_imports=False)


if __name__ == "__main__": print(json.dumps(check(sys.argv[1] if len(sys.argv)>1 else "."),indent=2))
