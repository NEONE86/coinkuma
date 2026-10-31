"""CASH/price controls and optional free-form Codex decision proposer.

Codex decisions are *advisory intents*. Only the chronology-checked PAPER
engine processes their actions. External model attempts are opt-in and bounded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .engine import Plan, ReplayError, canonical, sha256


def plan(action: str, symbol: str, fraction: float, thesis: str,
         *, p_up: float = 0.5) -> dict[str, Any]:
    return dict(action=action, symbol=symbol, fraction=fraction, p_up=p_up,
                thesis=thesis, counter_hypothesis="The observed movement may reverse.",
                invalidation="Reassess after the next fully received market packet.")


class CashPlanner:
    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        return plan("HOLD", "CASH", 0.0, "Zero-exposure cash control")


class MomentumPlanner:
    """Price-only *control*, not a validated trading strategy."""

    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        state = context["history"]
        returns = {s: v["current_mid"] / v["previous_mid"] - 1
                   for s, v in state.items() if v["previous_mid"] is not None}
        held = list(context["positions"])
        if held:
            symbol = held[0]
            if returns.get(symbol, 0) <= -0.003:
                return plan("SELL", symbol, 1.0, "Exit after negative recent return")
            return plan("HOLD", symbol, 0.0, "Maintain position while trend persists")
        if returns:
            symbol = max(returns, key=lambda s: (returns[s], s))
            if returns[symbol] >= 0.003:
                return plan("BUY", symbol, 0.10, "Recent price-strength baseline")
        return plan("HOLD", "CASH", 0.0, "No baseline momentum signal")


class ReversalPlanner:
    """Independent price-only mean-reversion *control*."""

    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        state = context["history"]
        returns = {s: v["current_mid"] / v["previous_mid"] - 1
                   for s, v in state.items() if v["previous_mid"] is not None}
        held = list(context["positions"])
        if held:
            symbol = held[0]
            if returns.get(symbol, 0) >= 0:
                return plan("SELL", symbol, 1.0, "Close after relative recovery")
            return plan("HOLD", symbol, 0.0, "Wait for reversal")
        if returns:
            symbol = min(returns, key=lambda s: (returns[s], s))
            if returns[symbol] <= -0.003:
                return plan("BUY", symbol, 0.10, "Price-only reversal baseline")
        return plan("HOLD", "CASH", 0.0, "No baseline reversal signal")


class OnlineLearnerPlanner:
    """An explicit, modest online-feedback comparator; not an alpha claim.

    Uses only own matured independent BUY markouts, with shrinkage and a
    prior-momentum candidate. No weights are fitted on yet-unobserved returns.
    """

    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        state = context["history"]
        observations = {s: v["current_mid"] / v["previous_mid"] - 1
                        for s, v in state.items() if v["previous_mid"] is not None}
        samples = {s: [] for s in state}
        for row in context["resolved_memory"]:
            if row["action"] == "BUY" and row["symbol"] in samples:
                samples[row["symbol"]].append(row["net_markout"])
        estimates = {s: (sum(values) / (len(values) + 4)) for s, values in samples.items()}
        held = list(context["positions"])
        if held:
            symbol = held[0]
            if observations.get(symbol, 0.0) < -0.003 or (len(samples[symbol]) >= 3
                                                           and estimates[symbol] < -0.001):
                return plan("SELL", symbol, 1.0, "Prior markouts or tape deterioration recommend exit")
            return plan("HOLD", symbol, 0.0, "Keep existing position; no confirmed adverse evidence")
        if not observations:
            return plan("HOLD", "CASH", 0.0, "No prior received tape for online learner")
        scores = {s: estimates[s] + max(-0.01, min(0.01, move)) * 0.5
                  for s, move in observations.items()}
        symbol = max(scores, key=lambda s: (scores[s], s))
        if scores[symbol] <= 0.002 or (len(samples[symbol]) >= 3 and estimates[symbol] < 0):
            return plan("HOLD", "CASH", 0.0, "No positive cost-aware learned candidate")
        fraction = min(0.125, 0.05 + max(0, scores[symbol]) * 2.0)
        return plan("BUY", symbol, fraction,
                    "Shrunken matured out-of-sample markout and current received momentum; "
                    f"observed_labels={len(samples[symbol])}; score={scores[symbol]:.6f}")


MODEL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "symbol": {"type": "string"},
        "fraction": {"type": "number", "minimum": 0, "maximum": 1},
        "p_up": {"type": "number", "minimum": 0, "maximum": 1},
        "thesis": {"type": "string"},
        "counter_hypothesis": {"type": "string"},
        "invalidation": {"type": "string"},
    },
    "required": ["action", "symbol", "fraction", "p_up", "thesis", "counter_hypothesis", "invalidation"],
}


MODEL_INSTRUCTION = """You are ONE's experimental autonomous portfolio decision intelligence.
Your sole objective is to improve net long-run capital growth without permanent
ruin. This is a CLOSED INPUT test: use only the observation packet, portfolio,
and previously matured feedback in FROZEN_CONTEXT. No external knowledge, tools,
web, or future prices. You may independently choose BUY, SELL or HOLD, the symbol,
and the desired equity fraction; a separate engine imposes actual portfolio and
execution constraints. SELL exits held spot assets; it is not a short position.
Prefer CASH/HOLD when expected net gain fails to cover execution costs. Never
invent liquidity, fills, causal proof, or memory. Distinguish observations from
inference, include a counter-hypothesis and a falsifiable invalidation condition.
Return exactly the response schema. Never attempt real exchange execution."""


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    """A model intent must be persistent before an external process starts."""
    data = canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise


class CodexPlanner:
    """Opt-in bounded model calls, with fail-closed uncertain retries.

    This subprocess uses the same read-only noninteractive control style as
    existing ONE experiments. No executable is bundled or installed here.
    Model prompts, outputs and cost evidence stay in a private runtime folder.
    """

    def __init__(self, *, runtime: Path, model: str, max_calls: int,
                 executable: str = "codex", timeout_seconds: int = 60):
        if not runtime.is_absolute() or not runtime.exists() or not runtime.is_dir():
            raise ReplayError("PRIVATE_MODEL_RUNTIME_MUST_EXIST")
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or not 0 < max_calls <= 10_000:
            raise ReplayError("MODEL_CALL_CAP")
        if not isinstance(timeout_seconds, int) or not 0 < timeout_seconds <= 300:
            raise ReplayError("MODEL_TIMEOUT_CAP")
        if not isinstance(model, str) or not model or len(model) > 100:
            raise ReplayError("MODEL_ID")
        self.runtime = runtime.resolve()
        self.executable = executable
        self.model = model
        self.max_calls = max_calls
        self.timeout_seconds = timeout_seconds
        self.call_count = 0

    def propose(self, context: dict[str, Any]) -> dict[str, Any] | None:
        if self.call_count >= self.max_calls:
            return None
        if context["pending_order"]:
            return plan("HOLD", "CASH", 0.0, "Pending order is awaiting a later quote")
        digest = sha256([self.model, context])
        folder = self.runtime / digest
        folder.mkdir(parents=True, exist_ok=True)
        intent = folder / "intent.json"
        result = folder / "result.json"
        if result.exists():
            stored = json.loads(result.read_text(encoding="utf-8"))
            if stored["input_hash"] != sha256(context):
                raise ReplayError("CACHED_MODEL_INPUT_MISMATCH")
            if stored["status"] != "CONNECTED_VALID":
                return None
            Plan.parse(stored["answer"], set(context["universe"]))
            return stored["answer"]
        if intent.exists():
            # The external call may have completed without an acknowledgement;
            # never launch a second model attempt for the same opaque request.
            raise ReplayError("MODEL_EXTERNAL_STATE_UNCERTAIN")
        if not shutil.which(self.executable):
            return None
        self.call_count += 1
        _write_exclusive(intent, dict(input_hash=sha256(context), model=self.model,
                                       created_wall_utc=__import__("datetime").datetime.now(
                                           __import__("datetime").timezone.utc).isoformat()))
        (folder / "schema.json").write_bytes(canonical(MODEL_SCHEMA))
        prompt = MODEL_INSTRUCTION + "\nFROZEN_CONTEXT:\n" + canonical(context).decode("utf-8")
        (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
        command = [self.executable, "exec", "--ignore-user-config", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--model", self.model,
                   "-c", "features.shell_tool=false", "-c", 'web_search="disabled"',
                   "--output-schema", str(folder / "schema.json"),
                   "--output-last-message", str(folder / "answer.json"), "--json", "-"]
        started = time.monotonic()
        answer = None
        status = "MODEL_NOT_CONNECTED"
        error = None
        try:
            proc = subprocess.run(command, input=prompt, text=True, encoding="utf-8",
                                  capture_output=True, timeout=self.timeout_seconds, cwd=folder)
            (folder / "stdout.jsonl").write_text(proc.stdout, encoding="utf-8")
            (folder / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
            if proc.returncode:
                raise ReplayError("MODEL_EXIT_NONZERO")
            for line in proc.stdout.splitlines():
                event = json.loads(line)
                if event.get("type") in ("item.started", "item.completed") and event.get("item", {}).get("type") not in ("agent_message", "reasoning"):
                    raise ReplayError("UNEXPECTED_MODEL_TOOL_USE")
            answer = json.loads((folder / "answer.json").read_text(encoding="utf-8"))
            Plan.parse(answer, set(context["universe"]))
            status = "CONNECTED_VALID"
        except subprocess.TimeoutExpired:
            status, error = "MODEL_TIMEOUT", "Timeout; no speculative decision"
        except (ValueError, OSError, ReplayError) as exc:
            status, error = "MODEL_INVALID_OUTPUT", str(exc)[:200]
            answer = None
        _write_exclusive(result, dict(input_hash=sha256(context), answer=answer,
                                       output_hash=sha256(answer) if answer is not None else None,
                                       status=status, error=error,
                                       elapsed_seconds=time.monotonic()-started,
                                       provider_cost_usdt="UNKNOWN_NOT_ZERO"))
        return answer
