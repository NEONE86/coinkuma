"""One-shot research CLI. Network collection is explicit; reports are offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

from . import VERSION
from .evidence import VersionedEvidence, observe, price_from_ticker
from .events import register_asset, register_event, register_flow
from .review import (audit, compare_policies, mark_due_missing, register_attribution,
                     register_change, register_policy, report, resolve_forecast, review_change, freeze_policy_cohort)
from .store import Store, atomic_bytes, digest, instant, nonempty, utc, write_json
from .theses import make_packet, register_bundle, register_decision, register_forecast
from .recovery import quarantine_question, recover_question


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def ingest(store, payload):
    dispatch = {"question_quarantine": quarantine_question, "question_recovery": recover_question,
                "forecast": register_forecast, "asset_identity": register_asset, "observation": observe,
                "economic_flow": register_flow, "economic_event_revision": register_event,
                "policy": register_policy, "policy_change": register_change,
                "policy_change_review": review_change, "attribution": register_attribution,
                "policy_cohort": freeze_policy_cohort,
                "valuation_and_decision": register_decision}
    result = []
    try:
        with store.transaction():
            for item in payload:
                kind, data = item["kind"], item["data"]
                if kind not in dispatch:
                    raise ValueError("unsupported_ingest_kind")
                result.append(dispatch[kind](store, data))
    except Exception as exc:
        store.attempt("ingest", payload, "REJECTED", str(exc))
        raise
    return result


def collect(store, sources, offline=False):
    evidence = VersionedEvidence(store, offline=offline)
    output = []
    for source in sources:
        try:
            raw, receipt = evidence.get(source["url"], refresh=not offline,
                                        source_kind=source.get("source_kind", "OFFICIAL_DOCUMENT"))
            row = dict(url=source["url"], status="CAPTURED" if not offline else "OFFLINE_REUSE",
                       snapshot_id=receipt["snapshot_id"], bytes=len(raw))
            if source.get("price_asset_id"):
                row["price_snapshot"] = price_from_ticker(store, source["price_asset_id"], receipt["snapshot_id"])
            output.append(row)
        except Exception as exc:
            row = dict(url=source["url"], status="FAILED", error=type(exc).__name__ + ": " + str(exc))
            store.attempt("collect", source, "REJECTED", row)
            output.append(row)
    return dict(status="PARTIAL_COVERAGE" if any(r["status"] == "FAILED" for r in output) else "COMPLETE",
                sources=output)


def candidate_comparison(store, payload):
    nonempty(payload, "comparison_id", "decision_ids", "pairwise_reasons", "next_single_action")
    decisions = [store.get("valuation_and_decision", did) for did in payload["decision_ids"]]
    if len({d["target_asset_id"] for d in decisions}) != len(decisions):
        raise ValueError("candidate_identity_duplicate")
    old = store.get("candidate_comparison", payload["comparison_id"], optional=True)
    data = dict(payload, registered_at=old["registered_at"] if old else utc())
    return store.put("candidate_comparison", payload["comparison_id"], data)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("init", "collect-once", "ingest", "packet", "register", "resolve", "compare", "report", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True, help="Runtime directory outside every Git checkout")
        command.add_argument("--output", type=Path)
        if name in {"ingest", "register"}:
            command.add_argument("--file", type=Path, required=True)
        elif name == "collect-once":
            command.add_argument("--sources", type=Path, required=True, help="JSON array of reviewed public URLs")
            command.add_argument("--offline", action="store_true")
        elif name == "packet":
            command.add_argument("--asset-id", action="append", required=True)
            command.add_argument("--as-of")
        elif name == "resolve":
            command.add_argument("--file", type=Path, help="Evidence-linked measured outcome; omitted = mark due missing")
            command.add_argument("--as-of")
        elif name == "compare":
            command.add_argument("--file", type=Path, required=True, help="Candidate reasons or frozen policy pair")
        elif name == "report":
            command.add_argument("--as-of")
            command.add_argument("--format", choices=("json", "markdown"), default="json")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store(args.workspace)
    started, run_id = utc(), uuid.uuid4().hex
    status, error, result = "COMPLETED", None, None
    try:
        if args.command == "init":
            result = dict(schema_version=VERSION, reasoner_backend="manual_import", execution_authority="NONE")
            store.put("workspace_config", "v4", result)
        elif args.command == "collect-once":
            result = collect(store, read_json(args.sources), args.offline)
            if result["status"] == "PARTIAL_COVERAGE":
                status = "PARTIAL_COVERAGE"
        elif args.command == "ingest":
            result = ingest(store, read_json(args.file))
        elif args.command == "packet":
            result = make_packet(store, args.asset_id, args.as_of)
        elif args.command == "register":
            result = register_bundle(store, read_json(args.file))
        elif args.command == "resolve":
            result = resolve_forecast(store, read_json(args.file)) if args.file else mark_due_missing(store, args.as_of)
        elif args.command == "compare":
            data = read_json(args.file)
            result = compare_policies(store, data["change_id"], data["decision_ids"]) if "change_id" in data else candidate_comparison(store, data)
        elif args.command == "report":
            result = report(store, args.as_of)
        elif args.command == "audit":
            result = audit(store)
            if result["status"] != "PASS":
                status = "FAILED"
        rendered = None
        if args.command == "report" and args.format == "markdown":
            from .render import markdown
            rendered = markdown(result)
        if args.output:
            if rendered is not None:
                atomic_bytes(args.output, rendered.encode("utf-8"))
            else:
                write_json(args.output, result)
            print(json.dumps(dict(status=status, output=str(args.output.resolve())), ensure_ascii=False))
        else:
            print(rendered if rendered is not None else json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        status, error = "FAILED", type(exc).__name__ + ": " + str(exc)
        print(json.dumps(dict(status=status, error=error), ensure_ascii=False), file=sys.stderr)
    finally:
        root = Path(__file__).resolve().parents[2]
        commit = subprocess.run(["git", "-c", "safe.directory=" + str(root), "rev-parse", "HEAD"],
                                cwd=root, capture_output=True, text=True, errors="replace").stdout.strip() or "UNKNOWN"
        code_hash = digest({str(p.relative_to(root)): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
                            for p in sorted(Path(__file__).parent.glob("*")) if p.is_file()})
        store.put("run_manifest", run_id, dict(run_id=run_id, code_commit=commit, code_content_hash=code_hash,
                  schema_version=VERSION, causal_model_version="see_thesis_revisions", estimation_version="see_decisions",
                  policy_version="see_decisions", asset_registry_hash=digest(store.all("asset_identity")),
                  evidence_manifest_hash=digest(store.all("source_snapshot")), model_id="manual_import",
                  prompt_hash=None, reasoner_backend="manual_import", model_call_status="NOT_RUN",
                  started_at=started, completed_at=utc(), data_cutoff=started, mode="ONE_SHOT",
                  command=args.command, status=status, error=error))
        store.close()
    return 1 if status == "FAILED" else 2 if status == "PARTIAL_COVERAGE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
