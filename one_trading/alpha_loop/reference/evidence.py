"""Offline, bounded-utility sequential evidence reference; never an order engine.

The receipt is a calculation, NOT authentication of market data or proof of raw
expected PnL. See STATISTICAL_PROTOCOL_REVIEW.txt for the conditional-null
assumptions. No networking, broker calls, key handling, or policy deployment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Mapping


def canonical_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone is required")
    return result.astimezone(timezone.utc)


def number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class EvidenceSpec:
    experiment_id: str
    version: str
    alpha_index: int
    registered_at: str
    mode: str
    policy: str
    baseline: str
    risk_fingerprint: str
    return_scale: float
    memory_comparator: str | None = None
    alpha_total: float = 0.05
    lambda_grid: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75)
    absolute_hurdle: float = 0.0
    incremental_hurdle: float = 0.0
    minimum_clusters: int = 30
    max_unit_loss: float = 0.10
    decision_contract: str = "PRESTART_OUTPUT"

    def __post_init__(self):
        if self.decision_contract not in {"PRESTART_OUTPUT", "PRESTART_RULE_AND_INPUT_NATIVE_OUTPUT_LATER"}:
            raise ValueError("unsupported decision contract")
        for name in ("experiment_id", "version", "policy", "baseline", "risk_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} required")
        if self.mode not in {"SYNTHETIC", "HISTORICAL", "FORWARD_PAPER"}:
            raise ValueError("unsupported evidence mode")
        if type(self.alpha_index) is not int or self.alpha_index < 1:
            raise ValueError("global immutable alpha_index must be positive integer")
        if type(self.minimum_clusters) is not int or self.minimum_clusters < 1:
            raise ValueError("minimum_clusters must be positive integer")
        instant(self.registered_at)
        if not 0 < number(self.alpha_total, "alpha_total") < 1:
            raise ValueError("alpha_total outside (0,1)")
        if not 0 < number(self.return_scale, "return_scale"):
            raise ValueError("return_scale must be positive")
        if not 0 < number(self.max_unit_loss, "max_unit_loss"):
            raise ValueError("max_unit_loss must be positive")
        for name in ("absolute_hurdle", "incremental_hurdle"):
            if not 0 <= number(getattr(self, name), name) < 1:
                raise ValueError(f"{name} outside [0,1)")
        if not isinstance(self.lambda_grid, tuple) or not self.lambda_grid:
            raise ValueError("lambda grid must be a nonempty immutable tuple")
        if any(not 0 < number(x, "lambda") < 1 for x in self.lambda_grid):
            raise ValueError("lambda outside (0,1)")
        labels = [self.policy, self.baseline]
        if self.memory_comparator is not None:
            if not isinstance(self.memory_comparator, str) or not self.memory_comparator:
                raise ValueError("invalid memory comparator")
            labels.append(self.memory_comparator)
        if len(set(labels)) != len(labels):
            raise ValueError("policies must be distinct")

    @property
    def alpha(self) -> float:
        return self.alpha_total / (self.alpha_index * (self.alpha_index + 1))

    @property
    def fingerprint(self) -> str:
        return canonical_hash(asdict(self))

    @property
    def policies(self) -> tuple[str, ...]:
        return (self.policy, self.baseline) + ((self.memory_comparator,) if self.memory_comparator else ())


@dataclass(frozen=True)
class Metrics:
    """All entries are fractions of the SAME preregistered evaluation capital.

    gross_return includes mark-to-market economics and signed funding PnL;
    trading_cost and information_cost are non-negative, never deducted twice.
    Ex ante cost allocation and portfolio accounting are upstream obligations.
    """
    gross_return: float
    trading_cost: float
    information_cost: float = 0.0

    def __post_init__(self):
        number(self.gross_return, "gross_return")
        if number(self.trading_cost, "trading_cost") < 0:
            raise ValueError("negative trading cost")
        if number(self.information_cost, "information_cost") < 0:
            raise ValueError("negative information cost")

    @property
    def net(self) -> float:
        result = self.gross_return - self.trading_cost - self.information_cost
        return number(result, "net_return")


class MixtureEvidence:
    """Fixed mixture weights; λ is never chosen after seeing an observation."""
    def __init__(self, lambdas: tuple[float, ...]):
        if not lambdas or any(not 0 < number(x, "lambda") < 1 for x in lambdas):
            raise ValueError("invalid lambdas")
        self.lambdas = tuple(lambdas)
        self.logs = [0.0] * len(lambdas)
        self.n = 0

    def update(self, x: float) -> None:
        x = number(x, "bounded increment")
        if not -1 <= x <= 1:
            raise ValueError("increment outside [-1,1]")
        self.logs = [v + math.log1p(lam * x) for v, lam in zip(self.logs, self.lambdas)]
        self.n += 1

    @property
    def log_e(self) -> float:
        m = max(self.logs)
        return m + math.log(sum(math.exp(v - m) for v in self.logs) / len(self.logs))

    def receipt(self, alpha: float) -> dict:
        if not 0 < number(alpha, "alpha") < 1:
            raise ValueError("invalid alpha")
        return {"n": self.n, "log_e": self.log_e, "log_threshold": -math.log(alpha),
                "threshold_crossed_now": self.log_e >= -math.log(alpha)}


class EvidenceLedger:
    """In-memory verifier rebuilt from immutable case/outcome records.

    Callers must persist original records and attest input authenticity elsewhere.
    Only the next chronological complete cluster advances the statistical count.
    Different registered experiments may share future clusters; within-experiment
    duplicate and overlapping clusters are rejected, not treated as new trials.
    """
    def __init__(self, spec: EvidenceSpec):
        self.spec = spec
        self.cases: list[dict] = []
        self.outcomes: dict[str, dict] = {}
        self.processed = 0
        self.evaluation_as_of = spec.registered_at
        self.streams = {"absolute": MixtureEvidence(spec.lambda_grid),
                        "vs_baseline": MixtureEvidence(spec.lambda_grid)}
        if spec.memory_comparator:
            self.streams["memory_increment"] = MixtureEvidence(spec.lambda_grid)
        self.raw: list[dict] = []

    def register_case(self, *, case_id: str, cluster_id: str, decision_at: str,
                      available_at: str, starts_at: str, due_at: str,
                      decision_hashes: Mapping[str, str], risk_fingerprint: str,
                      spec_hash: str) -> str:
        if spec_hash != self.spec.fingerprint:
            raise ValueError("spec version/hash mismatch")
        if risk_fingerprint != self.spec.risk_fingerprint:
            raise ValueError("unmatched risk/capital contract")
        if not case_id or not cluster_id:
            raise ValueError("case and cluster identities required")
        registered = instant(self.spec.registered_at)
        decision, available = instant(decision_at), instant(available_at)
        start, due = instant(starts_at), instant(due_at)
        if not registered < decision < start < due or available > decision:
            raise ValueError("decision/source chronology violation")
        if set(decision_hashes) != set(self.spec.policies) or any(
                not isinstance(v, str) or not v for v in decision_hashes.values()):
            raise ValueError("all frozen policy decisions are required")
        case = dict(case_id=case_id, cluster_id=cluster_id, decision_at=decision_at,
                    available_at=available_at, starts_at=starts_at, due_at=due_at,
                    decision_hashes=dict(decision_hashes), risk_fingerprint=risk_fingerprint,
                    spec_hash=spec_hash)
        for old in self.cases:
            if old["case_id"] == case_id:
                if old != case:
                    raise ValueError("case identity conflict")
                return canonical_hash(old)
            if old["cluster_id"] == cluster_id:
                raise ValueError("duplicate economic cluster")
        if decision <= instant(self.evaluation_as_of):
            raise ValueError("cannot preregister a decision before already observed outcomes")
        if self.cases and start < instant(self.cases[-1]["due_at"]):
            raise ValueError("overlapping or out-of-order unit")
        self.cases.append(case)
        return canonical_hash(case)

    def resolve(self, *, case_id: str, metrics: Mapping[str, Metrics],
                received_at: str, as_of: str, source_hash: str, mode: str) -> str:
        case = next((x for x in self.cases if x["case_id"] == case_id), None)
        if case is None:
            raise ValueError("unknown preregistered case")
        if mode != self.spec.mode:
            raise ValueError("mixed evidence origins")
        if not instant(case["due_at"]) <= instant(received_at) <= instant(as_of):
            raise ValueError("immature or future resolution")
        if instant(as_of) < instant(self.evaluation_as_of):
            raise ValueError("evaluation clock cannot move backwards")
        if set(metrics) != set(self.spec.policies):
            raise ValueError("partial comparison stays pending; no selective scoring")
        if not isinstance(source_hash, str) or not source_hash:
            raise ValueError("source hash required")
        if any(not isinstance(v, Metrics) for v in metrics.values()):
            raise ValueError("metrics must use the validated Metrics type")
        observation = {"case_id": case_id, "metrics": {k: asdict(v) for k, v in metrics.items()},
                       "received_at": received_at, "source_hash": source_hash, "mode": mode}
        prior = self.outcomes.get(case_id)
        if prior is not None:
            if prior != observation:
                raise ValueError("outcome conflict; corrections require new analysis not rewrite")
            return canonical_hash(prior)
        for value in metrics.values():
            value.net  # validate arithmetic before any mutation
        self.outcomes[case_id] = observation
        self.evaluation_as_of = as_of
        self._drain()
        return canonical_hash(observation)

    def _drain(self) -> None:
        while self.processed < len(self.cases):
            case = self.cases[self.processed]
            if case["case_id"] not in self.outcomes:
                return
            observation = self.outcomes[case["case_id"]]
            net = {k: Metrics(**v).net for k, v in observation["metrics"].items()}
            utility = {k: max(-1.0, min(1.0, v / self.spec.return_scale)) for k, v in net.items()}
            own = utility[self.spec.policy]
            h_abs, h_inc = self.spec.absolute_hurdle, self.spec.incremental_hurdle
            increments = {"absolute": (own - h_abs) / (1 + h_abs),
                          "vs_baseline": ((own - utility[self.spec.baseline]) / 2 - h_inc) / (1 + h_inc)}
            if self.spec.memory_comparator:
                increments["memory_increment"] = ((own - utility[self.spec.memory_comparator]) / 2 - h_inc) / (1 + h_inc)
            for label, increment in increments.items():
                self.streams[label].update(increment)
            self.raw.append({"case_id": case["case_id"], "cluster_id": case["cluster_id"],
                             "net": net, "bounded_utility": utility, "increments": increments})
            self.processed += 1

    def receipt(self) -> dict:
        streams = {k: stream.receipt(self.spec.alpha) for k, stream in self.streams.items()}
        enough = self.processed >= self.spec.minimum_clusters
        gates_pass = enough and all(x["threshold_crossed_now"] for x in streams.values())
        # Raw tail-risk checks are kept separate from the clipped statistical score.
        # ALL received outcomes count for safety, even behind a missing label.
        own_returns = [Metrics(**o["metrics"][self.spec.policy]).net for o in self.outcomes.values()]
        severe_losses = sum(r < -self.spec.max_unit_loss for r in own_returns)
        tail_blocked = severe_losses > 0
        if tail_blocked:
            state = "RAW_TAIL_LIMIT_BREACHED"
        elif self.spec.mode == "HISTORICAL":
            state = "HISTORICAL_EXPLORATION_ONLY"
        elif gates_pass:
            state = ("SYNTHETIC_THRESHOLD_DEMO" if self.spec.mode == "SYNTHETIC"
                     else "STATISTICAL_CANDIDATE_REVIEW_REQUIRED")
        else:
            state = "NEEDS_MORE_EVIDENCE"
        return {"experiment_id": self.spec.experiment_id, "spec_hash": self.spec.fingerprint,
                "evaluation_as_of": self.evaluation_as_of,
                "alpha_index": self.spec.alpha_index, "alpha": self.spec.alpha,
                "mode": self.spec.mode, "state": state,
                "statistical_gate_passed": gates_pass, "raw_tail_guard_passed": not tail_blocked,
                "registered_clusters": len(self.cases), "scored_clusters": self.processed,
                "unresolved_clusters": len(self.cases) - len(self.outcomes),
                "resolved_waiting_for_prior": len(self.outcomes) - self.processed,
                "streams": streams, "raw_net_sum_not_compounded": sum(own_returns),
                "raw_worst_received_cluster": min(own_returns) if own_returns else None,
                "raw_tail_breaches": severe_losses,
                "input_attestation_verified": False, "alpha_allocation_verified": False,
                "registration_time_verified": False, "edge_proven": False,
                "live_trading_enabled": False,
                "scope": "BOUNDED_UTILITY_CONDITIONAL_NULL_TEST_NOT_RAW_PNL_PROOF",
                "warning": "Same engine and statistical math can verify numbers, not their market authenticity or future persistence."}
