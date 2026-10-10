"""Evidence governance and *simulation-only* position proposals.

This module cannot place orders, enable live trading, change its source, change a
risk policy, or promote a strategy automatically. Thresholds are proposed research
defaults, not a live mandate and not evidence of predictive edge. ``MEASURED`` is
a caller's provenance attestation: the store preserves the linked measurement
artifact hash but does not inspect the artifact or certify the supplied metrics.

SQLite UPDATE/DELETE triggers protect the ordinary application path, not a user
with filesystem or database-administrator access. Keep independent backups if an
audit trail must withstand deliberate tampering.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


DISCLAIMER = (
    "PROPOSED SIMULATION DEFAULTS; HUMAN REVIEW ONLY. Passing these gates does "
    "not prove predictive edge, prevent losses, or authorize live trading. "
    "MEASURED provenance is caller-attested, not independently verified."
)


class Stage(str, Enum):
    BACKTEST = "BACKTEST"
    OOS = "OOS"
    PAPER = "PAPER"


class Provenance(str, Enum):
    SELF_REPORTED = "SELF_REPORTED"
    MEASURED = "MEASURED"


@dataclass(frozen=True)
class Costs:
    """Round-trip costs in basis points, explicit even when funding is zero.

    Estimates must be included in the linked metric calculation; simply filling
    out these fields does not subtract costs from an otherwise gross result.
    Funding is a conservative nonnegative cost estimate, not a credit forecast.
    """

    fees_bps: float | None = None
    slippage_bps: float | None = None
    funding_bps: float | None = None


@dataclass(frozen=True)
class GatePolicy:
    min_reward_risk: float = 3.0
    min_net_expectancy_r: float = 0.25
    min_profit_factor: float = 1.5
    # Win rate alone is not an edge. Keep it measured, without a 50% hurdle.
    min_win_rate: float = 0.0
    min_non_overlapping_samples: int = 50
    max_drawdown_fraction: float = 0.15
    max_drawdown_r: float = 12.0
    max_variants_per_experiment: int = 20
    min_challenger_improvement_r: float = 0.05


@dataclass(frozen=True)
class Evidence:
    run_id: str
    strategy_id: str
    stage: Stage | str
    dataset_sha256: str | None = None
    generated_at: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    train_end: str | None = None
    strategy_frozen_at: str | None = None
    holding_horizon_hours: float | None = None
    embargo_hours: float | None = None
    non_overlapping_samples: int | None = None
    mean_reward_risk: float | None = None
    net_expectancy_r: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    max_drawdown_fraction: float | None = None
    max_drawdown_r: float | None = None
    costs: Costs = field(default_factory=Costs)
    costs_included: bool | None = None
    no_lookahead_checked: bool | None = None
    point_in_time_universe_checked: bool | None = None
    non_overlap_checked: bool | None = None
    drawdown_from_marked_equity: bool | None = None
    provenance: Provenance | str = Provenance.SELF_REPORTED
    measurement_artifact_sha256: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        values = dict(data)
        raw_costs = values.get("costs")
        values["costs"] = Costs(**raw_costs) if isinstance(raw_costs, dict) else Costs()
        return cls(**values)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    status: str
    reasons: tuple[str, ...]
    disclaimer: str = DISCLAIMER


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _policy_errors(policy: GatePolicy) -> list[str]:
    errors = []
    for name, value in dataclasses.asdict(policy).items():
        if not _finite(value) or (value < 0 if name == "min_win_rate" else value <= 0):
            errors.append(f"invalid_gate_policy:{name}")
    for name in ("min_non_overlapping_samples", "max_variants_per_experiment"):
        if type(getattr(policy, name)) is not int:
            errors.append(f"invalid_gate_policy:{name}")
    for name in ("min_win_rate", "max_drawdown_fraction"):
        if _finite(getattr(policy, name)) and getattr(policy, name) > 1:
            errors.append(f"invalid_gate_policy:{name}")
    return errors


def evaluate(evidence: Evidence, policy: GatePolicy | None = None) -> GateResult:
    """Fail closed on incomplete, gross-only, overlapping or self-reported runs.

    Every accepted result is still provisional. Fifty non-overlapping samples is
    an initial gate, not a statistical guarantee; correlation, selection bias and
    repeated trials require separate analysis. A backtest never passes this gate.
    """
    policy = policy or GatePolicy()
    reasons = _policy_errors(policy)
    if reasons:
        return GateResult(False, "REJECTED", tuple(reasons))
    if not evidence.run_id or not evidence.strategy_id:
        reasons.append("missing_immutable_id")
    try:
        stage = Stage(evidence.stage)
    except (TypeError, ValueError):
        stage = None
        reasons.append("invalid_stage")
    if stage == Stage.BACKTEST:
        reasons.append("backtest_is_research_only")
    if evidence.provenance != Provenance.MEASURED:
        reasons.append("self_reported_or_missing_measurement_provenance")
    for name in ("dataset_sha256", "measurement_artifact_sha256"):
        if not _hash(getattr(evidence, name)):
            reasons.append(f"missing_or_invalid:{name}")

    times = {}
    for name in ("generated_at", "window_start", "window_end", "train_end", "strategy_frozen_at"):
        times[name] = _date(getattr(evidence, name))
        if times[name] is None:
            reasons.append(f"missing_or_invalid_aware_timestamp:{name}")
    if all(times.values()):
        if not times["window_start"] < times["window_end"] <= times["generated_at"]:
            reasons.append("invalid_measurement_time_order")
        if times["generated_at"] > datetime.now(timezone.utc) + timedelta(minutes=5):
            reasons.append("future_measurement_timestamp")
        if not times["strategy_frozen_at"] < times["window_start"]:
            reasons.append("strategy_not_frozen_before_test")
        if times["train_end"] > times["strategy_frozen_at"]:
            reasons.append("training_after_strategy_freeze")

    horizon_valid = _finite(evidence.holding_horizon_hours) and evidence.holding_horizon_hours > 0
    embargo_valid = _finite(evidence.embargo_hours) and evidence.embargo_hours >= 0
    if not horizon_valid:
        reasons.append("missing_or_invalid:holding_horizon_hours")
    if not embargo_valid:
        reasons.append("missing_or_invalid:embargo_hours")
    if horizon_valid and embargo_valid:
        if evidence.embargo_hours < evidence.holding_horizon_hours:
            reasons.append("embargo_shorter_than_holding_horizon")
        if times["train_end"] and times["window_start"]:
            try:
                embargo_end = times["train_end"] + timedelta(hours=evidence.embargo_hours)
                if not embargo_end < times["window_start"]:
                    reasons.append("training_test_overlap_or_insufficient_embargo")
            except OverflowError:
                reasons.append("invalid_embargo_duration")

    if type(evidence.non_overlapping_samples) is not int or evidence.non_overlapping_samples < policy.min_non_overlapping_samples:
        reasons.append("insufficient_non_overlapping_samples")
    for name in ("costs_included", "no_lookahead_checked", "point_in_time_universe_checked", "non_overlap_checked", "drawdown_from_marked_equity"):
        if getattr(evidence, name) is not True:
            reasons.append(f"not_verified:{name}")
    if not isinstance(evidence.costs, Costs):
        reasons.append("missing_cost_model")
    else:
        for name, value in dataclasses.asdict(evidence.costs).items():
            if not _finite(value) or value < 0:
                reasons.append(f"missing_or_invalid_cost:{name}")

    minima = {
        "mean_reward_risk": policy.min_reward_risk,
        "net_expectancy_r": policy.min_net_expectancy_r,
        "profit_factor": policy.min_profit_factor,
        "win_rate": policy.min_win_rate,
    }
    for name, minimum in minima.items():
        value = getattr(evidence, name)
        if not _finite(value) or value < minimum:
            reasons.append(f"below_minimum_or_missing:{name}")
    if _finite(evidence.win_rate) and evidence.win_rate > 1:
        reasons.append("invalid_win_rate")
    for name, maximum in (("max_drawdown_fraction", policy.max_drawdown_fraction), ("max_drawdown_r", policy.max_drawdown_r)):
        value = getattr(evidence, name)
        if not _finite(value) or value < 0 or value > maximum:
            reasons.append(f"above_maximum_or_missing:{name}")
    return GateResult(not reasons, "PROVISIONAL_PASS" if not reasons else "REJECTED", tuple(reasons))


class GovernanceStore:
    """Append-only research ledger; no strategy activation method exists."""

    def __init__(self, path: str | Path = ":memory:", policy: GatePolicy | None = None):
        self.policy = policy or GatePolicy()
        if _policy_errors(self.policy):
            raise ValueError("Invalid gate policy")
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS strategies (
                strategy_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                registered_at TEXT NOT NULL, parameters_json TEXT NOT NULL,
                parameters_sha256 TEXT NOT NULL, description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES strategies(strategy_id),
                recorded_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                evaluation_json TEXT NOT NULL, policy_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES strategies(strategy_id),
                recorded_at TEXT NOT NULL, outcome TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,
                challenger_id TEXT NOT NULL REFERENCES strategies(strategy_id),
                champion_id TEXT REFERENCES strategies(strategy_id), payload_json TEXT NOT NULL
            );
        """)
        for table in ("strategies", "evidence", "attempts", "proposals"):
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} "
                    f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END"
                )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GovernanceStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def register_strategy(self, strategy_id: str, parameters: Mapping[str, Any], *, experiment_id: str = "default", description: str = "") -> str:
        if not isinstance(strategy_id, str) or not strategy_id.strip() or not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError("Nonempty immutable strategy and experiment IDs are required")
        payload = _canonical(dict(parameters))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection:
            # Acquire the write lock before counting to avoid concurrent cap races.
            self.connection.execute("BEGIN IMMEDIATE")
            count = self.connection.execute("SELECT COUNT(*) FROM strategies WHERE experiment_id=?", (experiment_id,)).fetchone()[0]
            if count >= self.policy.max_variants_per_experiment:
                raise ValueError("Experiment variant cap reached; preregister a separately reviewed experiment")
            self.connection.execute("INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?)", (strategy_id, experiment_id, _utcnow(), payload, digest, description))
        return digest

    def record_attempt(self, strategy_id: str, outcome: str, details: Mapping[str, Any], *, attempt_id: str | None = None) -> str:
        if outcome not in {"REJECTED", "FAILED", "INCONCLUSIVE", "PROVISIONAL_PASS"}:
            raise ValueError("Unknown attempt outcome")
        attempt_id = attempt_id or str(uuid.uuid4())
        with self.connection:
            self.connection.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?)", (attempt_id, strategy_id, _utcnow(), outcome, _canonical(dict(details))))
        return attempt_id

    def record_evidence(self, evidence: Evidence) -> GateResult:
        """Store even failed runs, atomically with a losing/winning attempt entry.

        NaN/Infinity are rejected by canonical JSON; callers can still log such a
        failed experiment through ``record_attempt`` using a finite diagnostic.
        """
        result = evaluate(evidence, self.policy)
        payload = _canonical(dataclasses.asdict(evidence))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection:
            self.connection.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", (evidence.run_id, evidence.strategy_id, _utcnow(), payload, _canonical(dataclasses.asdict(result)), _canonical(dataclasses.asdict(self.policy)), digest))
            self.connection.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?)", (f"evidence:{evidence.run_id}", evidence.strategy_id, _utcnow(), result.status, _canonical({"run_id": evidence.run_id, "reasons": result.reasons})))
        return result

    def status(self) -> dict[str, Any]:
        return {
            "mode": "RESEARCH_AND_SIMULATION_ONLY",
            "live_trading_enabled": False,
            "strategies": self.connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0],
            "evidence_runs": self.connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "attempts": self.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            "promotion_proposals": self.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],
            "policy": dataclasses.asdict(self.policy),
            "disclaimer": DISCLAIMER,
        }

    def _latest_stages(self, strategy_id: str) -> dict[Stage, Evidence]:
        rows = self.connection.execute("SELECT evidence_json FROM evidence WHERE strategy_id=? ORDER BY rowid", (strategy_id,)).fetchall()
        found: dict[Stage, Evidence] = {}
        # Select by event time, not insertion order; a backfilled older success
        # cannot hide a more recent failed result. Ties use the latest insertion.
        for (raw,) in rows:
            evidence = Evidence.from_dict(json.loads(raw))
            try:
                stage = Stage(evidence.stage)
            except (TypeError, ValueError):
                continue
            previous = found.get(stage)
            current_end = _date(evidence.window_end)
            previous_end = _date(previous.window_end) if previous else None
            if previous is None or current_end is None or previous_end is None or current_end >= previous_end:
                found[stage] = evidence
        return found

    def propose_promotion(self, challenger_id: str, champion_id: str | None = None) -> dict[str, Any]:
        """Append a bounded human-review proposal; never change the champion.

        Both OOS and forward PAPER must pass. Comparing with an existing champion
        additionally requires matching stage datasets/windows and better PAPER
        net expectancy without worse drawdown. A first champion receives only a
        proposal for human review. No account or notional allocation is changed.
        """
        if not self.connection.execute("SELECT 1 FROM strategies WHERE strategy_id=?", (challenger_id,)).fetchone():
            raise KeyError(challenger_id)
        if champion_id is not None and not self.connection.execute("SELECT 1 FROM strategies WHERE strategy_id=?", (champion_id,)).fetchone():
            raise KeyError(champion_id)
        reasons: list[str] = []
        if champion_id == challenger_id:
            reasons.append("challenger_equals_champion")
        candidate = self._latest_stages(challenger_id)
        for stage in (Stage.OOS, Stage.PAPER):
            if stage not in candidate:
                reasons.append(f"missing_stage:{stage.value}")
            else:
                result = evaluate(candidate[stage], self.policy)
                reasons.extend(f"{stage.value}:{reason}" for reason in result.reasons)
        if Stage.OOS in candidate and Stage.PAPER in candidate:
            oos_end = _date(candidate[Stage.OOS].window_end)
            paper_start = _date(candidate[Stage.PAPER].window_start)
            if not oos_end or not paper_start or not oos_end < paper_start:
                reasons.append("paper_not_strictly_after_oos")
            if candidate[Stage.OOS].dataset_sha256 == candidate[Stage.PAPER].dataset_sha256:
                reasons.append("oos_and_paper_reuse_dataset")
        if champion_id is not None:
            champion = self._latest_stages(champion_id)
            for stage in (Stage.OOS, Stage.PAPER):
                if stage not in champion or stage not in candidate:
                    reasons.append(f"missing_comparable_stage:{stage.value}")
                    continue
                champion_result = evaluate(champion[stage], self.policy)
                reasons.extend(f"champion_{stage.value}:{reason}" for reason in champion_result.reasons)
                for name in ("dataset_sha256", "window_start", "window_end"):
                    if getattr(champion[stage], name) != getattr(candidate[stage], name):
                        reasons.append(f"not_comparable:{stage.value}:{name}")
            if Stage.PAPER in champion and Stage.PAPER in candidate:
                c, b = candidate[Stage.PAPER], champion[Stage.PAPER]
                if not (_finite(c.net_expectancy_r) and _finite(b.net_expectancy_r) and c.net_expectancy_r >= b.net_expectancy_r + self.policy.min_challenger_improvement_r):
                    reasons.append("insufficient_challenger_improvement")
                for name in ("max_drawdown_fraction", "max_drawdown_r"):
                    cv, bv = getattr(c, name), getattr(b, name)
                    if not (_finite(cv) and _finite(bv) and cv <= bv):
                        reasons.append(f"challenger_drawdown_not_better_or_equal:{name}")
        payload = {
            "proposal_id": str(uuid.uuid4()),
            "challenger_id": challenger_id,
            "champion_id": champion_id,
            "status": "HUMAN_REVIEW_REQUIRED" if not reasons else "REJECTED",
            "reasons": reasons,
            "run_ids": {stage.value: item.run_id for stage, item in candidate.items()},
            "policy": dataclasses.asdict(self.policy),
            "live_trading_enabled": False,
            "disclaimer": DISCLAIMER,
        }
        with self.connection:
            self.connection.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?)", (payload["proposal_id"], _utcnow(), challenger_id, champion_id, _canonical(payload)))
        return payload


@dataclass(frozen=True)
class RiskPolicy:
    risk_per_trade_fraction: float = 0.005
    max_open_stop_risk_fraction: float = 0.03
    max_daily_marked_loss_fraction: float = 0.03
    max_drawdown_fraction: float = 0.15
    max_gross_notional_to_equity: float = 1.0
    min_net_reward_risk: float = 3.0


@dataclass(frozen=True)
class PositionRequest:
    equity: float
    entry_price: float
    stop_price: float
    target_price: float
    day_start_equity: float
    high_water_equity: float
    roundtrip_cost_fraction: float
    side: str = "LONG"
    requested_risk_fraction: float = 0.005
    existing_open_stop_risk_fraction: float = 0.0
    existing_gross_notional: float = 0.0
    max_notional: float | None = None


@dataclass(frozen=True)
class PositionProposal:
    allowed: bool
    reasons: tuple[str, ...]
    notional: float = 0.0
    quantity: float = 0.0
    modeled_stop_loss: float = 0.0
    risk_fraction: float = 0.0
    net_reward_risk: float = 0.0
    mode: str = "SIMULATION_ONLY"
    disclaimer: str = (
        "Proposed simulation policy, not approved for live capital. Stops and "
        "estimated costs do not bound losses through gaps, outages or liquidation."
    )


def size_position(request: PositionRequest, policy: RiskPolicy | None = None) -> PositionProposal:
    """Size from marked equity and stop distance plus explicit round-trip cost.

    notional = equity * risk_fraction / (stop_fraction + roundtrip_cost)

    Existing stop-risk must be expressed against *current marked equity* and
    include expected closing costs. Aggregate notional includes both directions;
    opposite positions are not assumed to offset risk. No order is submitted.
    """
    policy = policy or RiskPolicy()
    reasons = []
    for name, value in dataclasses.asdict(policy).items():
        if not _finite(value) or value <= 0:
            reasons.append(f"invalid_risk_policy:{name}")
    for name in ("risk_per_trade_fraction", "max_open_stop_risk_fraction", "max_daily_marked_loss_fraction", "max_drawdown_fraction"):
        value = getattr(policy, name)
        if _finite(value) and value >= 1:
            reasons.append(f"invalid_risk_policy:{name}")
    for name, value in dataclasses.asdict(request).items():
        if name == "side" or (name == "max_notional" and value is None):
            continue
        if not _finite(value):
            reasons.append(f"nonfinite_or_missing:{name}")
    if reasons:
        return PositionProposal(False, tuple(reasons))
    for name in ("equity", "entry_price", "stop_price", "target_price", "day_start_equity", "high_water_equity", "requested_risk_fraction"):
        if getattr(request, name) <= 0:
            reasons.append(f"not_positive:{name}")
    for name in ("roundtrip_cost_fraction", "existing_open_stop_risk_fraction", "existing_gross_notional"):
        if getattr(request, name) < 0:
            reasons.append(f"negative:{name}")
    if request.max_notional is not None and request.max_notional <= 0:
        reasons.append("not_positive:max_notional")
    if request.roundtrip_cost_fraction >= 1:
        reasons.append("invalid_roundtrip_cost_fraction")
    if request.requested_risk_fraction > policy.risk_per_trade_fraction:
        reasons.append("per_trade_risk_cap_exceeded")
    if request.high_water_equity < max(request.equity, request.day_start_equity):
        reasons.append("high_water_equity_inconsistent")
    if request.side == "LONG":
        if not request.stop_price < request.entry_price < request.target_price:
            reasons.append("invalid_long_stop_or_target")
    elif request.side == "SHORT":
        if not request.target_price < request.entry_price < request.stop_price:
            reasons.append("invalid_short_stop_or_target")
    else:
        reasons.append("invalid_side")
    if reasons:
        return PositionProposal(False, tuple(reasons))
    daily_loss = (request.day_start_equity - request.equity) / request.day_start_equity
    drawdown = (request.high_water_equity - request.equity) / request.high_water_equity
    if daily_loss >= policy.max_daily_marked_loss_fraction:
        reasons.append("daily_marked_equity_loss_limit")
    if drawdown >= policy.max_drawdown_fraction:
        reasons.append("marked_equity_drawdown_limit")
    room_risk = policy.max_open_stop_risk_fraction - request.existing_open_stop_risk_fraction
    if room_risk <= 0:
        reasons.append("aggregate_open_stop_risk_limit")
    room_notional = request.equity * policy.max_gross_notional_to_equity - request.existing_gross_notional
    if room_notional <= 0:
        reasons.append("aggregate_notional_limit")
    stop_fraction = abs(request.entry_price - request.stop_price) / request.entry_price
    reward_fraction = abs(request.target_price - request.entry_price) / request.entry_price
    loss_fraction = stop_fraction + request.roundtrip_cost_fraction
    reward_risk = (reward_fraction - request.roundtrip_cost_fraction) / loss_fraction
    if not _finite(reward_risk) or reward_risk < policy.min_net_reward_risk:
        reasons.append("insufficient_net_reward_risk")
    if reasons:
        return PositionProposal(False, tuple(reasons))
    risk_fraction = min(request.requested_risk_fraction, room_risk)
    unconstrained = request.equity * risk_fraction / loss_fraction
    notional = min(unconstrained, room_notional, request.max_notional if request.max_notional is not None else room_notional)
    quantity = notional / request.entry_price
    modeled_loss = notional * loss_fraction
    if not all(_finite(v) and v > 0 for v in (notional, quantity, modeled_loss)):
        return PositionProposal(False, ("invalid_computed_position",))
    return PositionProposal(True, (), notional, quantity, modeled_loss, modeled_loss / request.equity, reward_risk)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status",))
    parser.add_argument("--db", default="governance.sqlite3")
    args = parser.parse_args()
    with GovernanceStore(args.db) as store:
        print(json.dumps(store.status(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
