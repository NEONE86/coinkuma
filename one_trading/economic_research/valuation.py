"""Decimal single-token scenarios, never account returns or simulated fills."""
from decimal import Decimal, localcontext

from .store import decimal, instant, nonempty, one_of, required


def probability(value):
    p = decimal(value)
    if not 0 <= p <= 1:
        raise ValueError("probability_out_of_range")
    return p


def leaves(tree):
    one_of(tree.get("kind"), {"JOINT", "CONDITIONAL"})
    nodes = tree.get("scenarios", [])
    if not nodes or len({n["id"] for n in nodes}) != len(nodes):
        raise ValueError("empty_or_duplicate_scenarios")
    if tree["kind"] == "JOINT":
        if any(n.get("parent_id") is not None for n in nodes):
            raise ValueError("joint_scenario_has_parent")
        if sum(probability(n["p"]) for n in nodes) != 1:
            raise ValueError("probability_sum")
        return [(n, probability(n["p"])) for n in nodes]
    by_id = {n["id"]: n for n in nodes}
    children = {}
    for n in nodes:
        required(n, "parent_id")
        parent = n["parent_id"]
        if parent is not None and parent not in by_id:
            raise ValueError("conditional_parent_missing")
        children.setdefault(parent, []).append(n)
    visited, result = set(), []

    def walk(parent, weight):
        group = children.get(parent, [])
        if sum(probability(n["p"]) for n in group) != 1:
            raise ValueError("conditional_probability_sum")
        for n in group:
            if n["id"] in visited:
                raise ValueError("conditional_cycle")
            visited.add(n["id"])
            joint = weight * probability(n["p"])
            if n["id"] in children:
                walk(n["id"], joint)
            else:
                result.append((n, joint))

    walk(None, Decimal(1))
    if len(visited) != len(nodes):
        raise ValueError("unreachable_conditional_node")
    return result


def value_scenarios(entry, tree, costs, horizon, quote_currency, required_return="0"):
    with localcontext() as context:
        context.prec = 80
        entry = decimal(entry)
        if entry <= 0:
            raise ValueError("nonpositive_entry")
        nonempty(costs, "basis", "unit")
        if costs["unit"] != "FRACTION_OF_NOTIONAL":
            raise ValueError("cost_unit")
        buy, sell = probability(costs["buy_fraction"]), probability(costs["sell_fraction"])
        if buy >= 1 or sell >= 1:
            raise ValueError("invalid_cost")
        hurdle = decimal(required_return)
        if hurdle <= -1:
            raise ValueError("invalid_required_return")
        results = []
        terminal_value = Decimal(0)
        for scenario, weight in leaves(tree):
            nonempty(scenario, "economic_assumptions", "valuation_basis", "quote_currency", "horizon")
            required(scenario, "terminal_price", "terminal_token_supply", "cash_distribution")
            if instant(scenario["horizon"]) != instant(horizon):
                raise ValueError("mixed_scenario_horizons")
            if scenario["quote_currency"] != quote_currency:
                raise ValueError("currency_mismatch")
            terminal, distribution = decimal(scenario["terminal_price"]), decimal(scenario["cash_distribution"])
            if terminal < 0 or distribution < 0:
                raise ValueError("negative_terminal_or_distribution")
            if scenario["terminal_token_supply"] is not None:
                if decimal(scenario["terminal_token_supply"]) <= 0:
                    raise ValueError("invalid_terminal_supply")
            elif not scenario.get("supply_missing_reason"):
                raise ValueError("missing_terminal_supply_reason")
            if distribution and scenario.get("distribution_kind") != "CASH":
                raise ValueError("reward_tokens_are_not_cash")
            if scenario.get("double_counts_buyback_cashflow", False):
                raise ValueError("double_counted_value_capture")
            net = terminal * (1 - sell) + distribution
            terminal_value += weight * net
            r = net / (entry * (1 + buy)) - 1
            results.append(dict(id=scenario["id"], joint_probability=str(weight), return_fraction=str(r)))
        expected = sum(decimal(r["joint_probability"]) * decimal(r["return_fraction"]) for r in results)
        breakeven = terminal_value / (1 + buy)
        required_price = breakeven / (1 + hurdle)
        sensitivity = [{"entry_price": str(entry * multiple),
                        "expected_return": str(breakeven / (entry * multiple) - 1)}
                       for multiple in map(Decimal, ("0.75", "1", "1.25", "2"))]
        ordered = sorted(results, key=lambda r: decimal(r["return_fraction"]))
        # A diagnostic shift in joint leaf mass, not independent marginal multiplication.
        transferred = min(Decimal("0.10"), decimal(ordered[-1]["joint_probability"]))
        adverse_probability = expected + transferred * (decimal(ordered[0]["return_fraction"]) - decimal(ordered[-1]["return_fraction"]))
        stress_buy, stress_sell = max(buy, Decimal("0.01")), max(sell, Decimal("0.01"))
        stressed_terminal = sum(weight * (decimal(scenario["terminal_price"]) * (1 - stress_sell) + decimal(scenario["cash_distribution"]))
                                for scenario, weight in leaves(tree))
        return dict(expected_return=str(expected), break_even_entry=str(breakeven),
                    entry_for_required_return=str(required_price), required_return=str(hurdle),
                    worst_scenario_return=str(min(decimal(r["return_fraction"]) for r in results)),
                    meets_required_return=expected >= hurdle, scenarios=results,
                    probability_sensitivity=dict(assumption="Move up to 10 percentage points of joint mass from best to worst leaf", transferred_mass=str(transferred),
                                                 expected_return=str(adverse_probability)),
                    cost_sensitivity=dict(assumption="At least 1% cost per side, diagnostic only", buy_fraction=str(stress_buy), sell_fraction=str(stress_sell),
                                          expected_return=str(stressed_terminal / (entry * (1 + stress_buy)) - 1)),
                    price_sensitivity=sensitivity, accounting="SINGLE_TOKEN_MARKOUT_NOT_PORTFOLIO_PNL")
