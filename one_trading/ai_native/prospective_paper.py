"""Read-only bridge from verified decision timing to the existing PAPER engine.

No network, inference, live orders, or source-file mutation.  A later quote can
fill only after the *recorded proposal-validation time* plus execution delay.
The old engine/auditor own the actual conditional fills and capital accounting.
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

from .audit import verify as verify_paper
from .engine import Experiment, ModelDecision, Protocol, ReplayError, instant
from .planners import CashPlanner, MomentumPlanner
from .prospective_audit import verify as verify_prospective


class RecordedPlanner:
    def __init__(self, by_frame: dict[str, ModelDecision]):
        self.by_frame = by_frame

    def propose(self, context: dict):
        # A missing request is NOT silently relabeled as a successful CASH call.
        return self.by_frame.get(context["frame_id"])


def replay(path: Path, *, source_dir: Path | None = None) -> dict:
    """Replay accepted, *already recorded* decisions; separately charge model costs.

    Evidence classification stays non-economic and requires prospective external
    model invocations on a separate, approved run to assess any real advantage.
    """
    evidence = verify_prospective(path, source_dir=source_dir)
    with Path(path).open("r", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    header = rows[0]["payload"]
    policy_entries = header["registry"]
    if "PRICE_ONLY" in policy_entries and policy_entries["PRICE_ONLY"] != {
        "version": "momentum-v1", "model": None, "prompt_sha256": None,
        "call_limit": 0, "cost_limit_usdt": "0"
    }:
        raise ReplayError("UNPINNED_PRICE_CONTROL")
    requested = {}
    frames = []
    frames_by_id = {}
    decisions = {}
    for row in rows[1:]:
        payload = row["payload"]
        if row["kind"] == "input_frozen":
            frames.append(payload["frame"])
            frames_by_id[payload["frame"]["id"]] = payload["frame"]
        elif row["kind"] == "decision_requested":
            requested[payload["request_id"]] = payload
        elif row["kind"] == "proposal_validated":
            req = requested[payload["request_id"]]
            source_frame = frames_by_id[req["frame_id"]]
            decision_ready = instant(row["at"])
            latency = (decision_ready - instant(source_frame["received_at"])).total_seconds()
            if latency < 0:
                raise ReplayError("PRE_RECEIPT_PROPOSAL")
            decisions.setdefault(req["policy_id"], {})[req["frame_id"]] = ModelDecision(
                payload["plan"], latency)
    if len(frames) < 2:
        raise ReplayError("INSUFFICIENT_PAPER_RECEIPTS")
    planners = {"CASH": CashPlanner()}
    if "PRICE_ONLY" in policy_entries:
        planners["PRICE_ONLY"] = MomentumPlanner()
    for policy, registered in policy_entries.items():
        if registered["model"] is not None:
            planners[policy] = RecordedPlanner(decisions.get(policy, {}))
    engine = Experiment(Protocol(execution_delay_seconds=header["execution_delay_seconds"]),
                        planners, data_use="SYNTHETIC" if header["source_class"] == "SYNTHETIC"
                        else "PROSPECTIVE_REPLAY")
    for frame in frames:
        engine.observe(frame)
    paper = engine.finish()
    paper_audit = verify_paper(paper)
    # Model costs are reported separately, and are not silently counted as
    # free. Existing PAPER positions do NOT size against these model fees.
    summary = {arm: {"paper_pnl_usdt": values["pnl_marked_at_bid"],
                      "model_spend_usdt": evidence["model_cost_usdt"].get(arm, "0"),
                      "cost_adjusted_pnl_usdt": (values["pnl_marked_at_bid"] -
                          float(evidence["model_cost_usdt"].get(arm, "0"))),
                      "fills": values["fills"], "model_unavailable_frames": values["model_unavailable"]}
               for arm, values in paper["summary"].items()}
    return {"status": "PASS_OFFLINE_REPLAY_ONLY", "source_evidence": evidence["status"],
            "paper_independent_audit": paper_audit,
            "data_use": paper["data_use"], "summary": summary,
            "cost_method": "POSTHOC_MODEL_COST_NOT_DEBITED_FROM_PAPER_SIZING",
            "economic_alpha": "NOT_ESTABLISHED", "live_orders": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = replay(args.ledger, source_dir=args.source_dir)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

