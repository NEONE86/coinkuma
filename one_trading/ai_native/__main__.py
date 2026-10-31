"""Finite AI-native PAPER experiments; no market collector or order router."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .audit import AuditError, verify
from .engine import Experiment, Protocol, ReplayError, canonical
from .planners import (CashPlanner, CodexPlanner, MomentumPlanner,
                       OnlineLearnerPlanner, ReversalPlanner)


REPO = Path(__file__).resolve().parents[2]


def require_private_output(filename: str) -> Path:
    path = Path(filename).expanduser().resolve()
    if path == REPO or REPO in path.parents:
        raise ReplayError("RESEARCH_OUTPUT_MUST_STAY_OUTSIDE_GIT_REPOSITORY")
    if not path.parent.is_dir():
        raise ReplayError("OUTPUT_DIRECTORY_MUST_ALREADY_EXIST")
    return path


def run(args: argparse.Namespace) -> dict:
    output = require_private_output(args.output)
    if output.exists():
        raise ReplayError("OUTPUT_EXISTS_NO_OVERWRITE")
    arms = {"CASH": CashPlanner(), "MOMENTUM_CONTROL": MomentumPlanner(),
            "REVERSAL_CONTROL": ReversalPlanner(), "ONLINE_LEARNER_CONTROL": OnlineLearnerPlanner()}
    if args.ai_mode == "codex":
        if args.private_model_runtime is None or args.max_ai_calls is None:
            raise ReplayError("AI_OPT_IN_NEEDS_PRIVATE_RUNTIME_AND_CALL_LIMIT")
        arms["AI_FREE_DECISION"] = CodexPlanner(runtime=Path(args.private_model_runtime),
                                                model=args.model, max_calls=args.max_ai_calls,
                                                executable=args.model_executable,
                                                timeout_seconds=args.model_timeout_seconds)
    engine = Experiment(Protocol(), arms, data_use=args.data_use)
    with open(args.input, encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if line.strip():
                try:
                    raw = json.loads(line)
                except ValueError as exc:
                    raise ReplayError("INVALID_JSONL_AT_LINE:" + str(line_no)) from exc
                engine.observe(raw)
    result = engine.finish()
    result["model_mode"] = args.ai_mode
    result["model_invocation_authorized"] = args.ai_mode == "codex"
    # Do not commit or publish this private evidence; validate before saving.
    verify(result)
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as dest:
        dest.write(canonical(result) + b"\n")
        dest.flush()
        os.fsync(dest.fileno())
    return {"saved": str(output), "evidence_class": result["evidence_class"],
            "summary": result["summary"], "model_mode": args.ai_mode,
            "independent_audit": "PASS"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m one_trading.ai_native",
                                     description="Offline, isolated multi-arm PAPER decision replay")
    commands = parser.add_subparsers(dest="command", required=True)
    trial = commands.add_parser("run", help="Offline sequential replay with CASH and price controls")
    trial.add_argument("--input", required=True, help="Frozen received quote packet JSONL")
    trial.add_argument("--output", required=True, help="New private JSON result outside checkout")
    trial.add_argument("--data-use", required=True,
                       choices=["SYNTHETIC", "REUSED_HISTORICAL", "PROSPECTIVE_REPLAY"])
    trial.add_argument("--ai-mode", choices=["off", "codex"], default="off")
    trial.add_argument("--private-model-runtime", help="Preexisting isolated model call folder")
    trial.add_argument("--max-ai-calls", type=int, help="Required bounded model budget")
    trial.add_argument("--model", default="gpt-6-astra", help="Provider model identifier, explicit opt-in only")
    trial.add_argument("--model-executable", default="codex",
                       help="Existing authenticated Codex executable (absolute path on Windows if PATH is missing)")
    trial.add_argument("--model-timeout-seconds", type=int, default=60,
                       help="Bounded per-call timeout, 1–300 seconds")
    audit = commands.add_parser("verify", help="Independent offline audit of a saved result")
    audit.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run(args)
        else:
            with open(args.input, encoding="utf-8") as stream:
                result = verify(json.load(stream))
    except (ReplayError, AuditError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
