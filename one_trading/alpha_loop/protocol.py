"""Frozen research defaults; no live capital, fitted probabilities or order API."""
from copy import deepcopy
from pathlib import Path
import hashlib

PROTOCOL = {
    "version": 2, "mechanism": "CF_RESIDUAL_FORCED_FLOW",
    "universe": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "arms": ["CF_PRICE_ONLY", "CF_SEMANTIC", "CF_SEMANTIC_MEMORY", "CF_CASH"],
    "horizon_seconds": 21600, "fit_hours": 168, "entry_abs_z": 1.5,
    "normalization_abs_z": .25, "stop_fraction": .02, "probe_risk_fraction": .0025,
    "free_research_parameters": ["horizon_seconds", "fit_hours", "entry_abs_z",
                                 "normalization_abs_z", "stop_fraction", "probe_risk_fraction"],
    "parameter_status": "RESEARCH_DEFAULT_NOT_OPTIMIZED",
    "capital_usdt": 10000, "max_exposure_fraction": .125,
    "instrument": "SPOT_LONG", "fee_bps_per_side": 10, "slippage_bps_per_side": 5,
    "max_spread_bps": 20, "max_book_impact_bps": 50, "book_participation": .01,
    "max_input_age_seconds": 10, "execution_delay_seconds": 2,
    "decision_validity_seconds": 600, "stream_seconds": 120,
    "max_event_delay_seconds": 10, "missing_grace_seconds": 3600,
    "flow_ratio_threshold": .5, "minimum_clusters": 30, "return_scale": .05,
    "max_unit_loss": .10, "cash_yield": 0, "funding": "NOT_APPLICABLE_SPOT",
    "borrow": "NOT_APPLICABLE_NO_SHORT", "gas": "NOT_APPLICABLE_CEX",
    "information_cost_usdt_per_model_call": .10,
    "information_cost_status": "RESEARCH_SENSITIVITY_ASSUMPTION_NOT_PROVIDER_INVOICE_OR_VERIFIED_UPPER_BOUND",
    "max_model_attempts": 3, "model_retry_seconds": [60, 300],
    "model_backend": "codex", "model_executable": "codex", "model_name": "gpt-6-astra",
    "model_timeout_seconds": 120, "model_prompt_version": "alpha-native-time-v1",
    "research_days": 14, "research_time_budget_seconds": 86400,
    "new_paid_commitments_allowed": False, "live_trading_enabled": False,
    "comparison": "NATIVE_DECISION_TIME_PRIMARY_MATCHED_TIME_DIAGNOSTIC",
    "benchmark": "OLS_EQUAL_WEIGHT_OTHER_TWO_ASSETS_BEFORE_EVENT",
    "economic_predicate": "Same-side liquidated base units in a complete 120s window starting no earlier than common end <= 0.5 * initial complete-window units; zero initial units or incomplete coverage => UNKNOWN. This measures observed venue flow exhaustion, not the cause of spot returns.",
    "causal_assumption": "Bybit forced inventory flow may spill into Binance spot through arbitrage. Direct causation and aggregate market coverage are unproven.",
    "counter_hypothesis": "New information and voluntary flows cause continuation; liquidation is a consequence, not a temporary mispricing.",
    "statistical_scope": "EXPLORATORY_CONDITIONAL_NULL_NOT_EMPIRICALLY_ESTABLISHED",
    "memory_claim_activation": "Allocate a distinct global alpha claim only before the first case with eligible real matured memory; earlier MEMORY_UNAVAILABLE arms remain preserved observational records, not zero returns or retrospectively scored cases.",
}

def defaults():
    return deepcopy(PROTOCOL)

def source_hash():
    root = Path(__file__).resolve().parent
    files = sorted(root.rglob("*.py"))
    # Reused execution/collection code is part of the deployed identity too.
    files += sorted((root.parent / "autonomy").glob("*.py"))
    files += sorted((root.parent / "economic_research").glob("*.py"))
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root.parent)).replace("\\", "/").encode())
        h.update(p.read_bytes())
    return h.hexdigest()
