"""Exact finite-scenario expected value of sample information (offline).

The calculation is exact GIVEN the supplied probabilities and utilities.
Those inputs are assumptions unless externally calibrated; this module does not
claim to estimate real investment skill, and its output never authorizes trades.
"""
from __future__ import annotations
from math import fsum, isclose, isfinite
from typing import Mapping


def finite(x, label):
    if isinstance(x, bool) or not isinstance(x, (float, int)) or not isfinite(x):
        raise ValueError(f"{label} must be finite numeric")
    return float(x)


def distribution(values, label):
    if not values or any(finite(x, label) < 0 for x in values):
        raise ValueError(f"invalid {label}")
    if not isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError(f"{label} must sum to 1; do not silently normalize")


def decision_value(prior: Mapping[str, float], actions: Mapping[str, Mapping[str, float]]) -> dict:
    distribution(list(prior.values()), "prior")
    if not actions:
        raise ValueError("at least one action, including no-trade, is required")
    if "NO_TRADE" not in actions or any(v != 0 for v in actions["NO_TRADE"].values()):
        raise ValueError("NO_TRADE must be explicitly available with zero incremental utility")
    utilities = {}
    for name, payoff in actions.items():
        if set(payoff) != set(prior):
            raise ValueError("scenario mismatch")
        utilities[name] = finite(fsum(prior[state] * finite(payoff[state], "utility") for state in prior),
                                 "expected utility")
    # A zero-value tie must not induce a trade just because its name sorts first.
    chosen = min(utilities, key=lambda name: (-utilities[name], name != "NO_TRADE", name))
    return {"action": chosen, "value": utilities[chosen], "action_values": utilities}


def evaluate_experiment(*, prior, actions, likelihoods, experiment_cost: float,
                        delay_cost: float = 0.0, input_status: str = "ASSUMED") -> dict:
    """likelihoods maps state -> {observable signal: P(signal | state)}.

    All cost values use the SAME utility units as actions. delay_cost is an
    explicit configured opportunity-cost assumption, not seconds added to money.
    """
    before = decision_value(prior, actions)
    if set(likelihoods) != set(prior):
        raise ValueError("likelihood scenario mismatch")
    signals = set(next(iter(likelihoods.values())))
    for row in likelihoods.values():
        if set(row) != signals:
            raise ValueError("likelihood signal mismatch")
        distribution(list(row.values()), "conditional likelihood")
    if input_status not in {"ASSUMED", "EXTERNALLY_CALIBRATED"}:
        raise ValueError("invalid input status")
    costs = finite(finite(experiment_cost, "experiment_cost") + finite(delay_cost, "delay_cost"), "total cost")
    if experiment_cost < 0 or delay_cost < 0:
        raise ValueError("negative experiment/delay cost")
    posterior_actions = {}
    gains = []
    for signal in sorted(signals):
        prob = fsum(prior[s] * likelihoods[s][signal] for s in prior)
        if prob == 0:
            continue
        posterior = {s: prior[s] * likelihoods[s][signal] / prob for s in prior}
        decision = decision_value(posterior, actions)
        # Stable joint-probability advantage, avoiding subtraction of two huge
        # expected values. Unchanged decisions yield exactly zero information gain.
        gains.append(finite(fsum(prior[s] * likelihoods[s][signal] *
                           finite(actions[decision["action"]][s] - actions[before["action"]][s], "utility difference")
                           for s in prior), "signal information gain"))
        posterior_actions[signal] = {"probability": prob, "posterior": posterior, **decision}
    evsi = max(0.0, finite(fsum(gains), "evsi"))
    after = finite(before["value"] + evsi, "value after observation")
    return {"before": before, "expected_value_after_observation": after,
            "evsi": evsi, "cost": costs, "net_decision_value": evsi - costs,
            "candidate_for_research": evsi > costs,
            "input_status": input_status, "input_calibration_verified": False,
            "posterior_actions": posterior_actions,
            "live_trading_enabled": False,
            "warning": "Exact conditional arithmetic; not an estimated probability of market alpha."}
