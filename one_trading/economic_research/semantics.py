"""Versioned semantic identities, independent of immutable serialized bodies."""
from .store import decimal, digest, stamp

FACT_IDENTITY_VERSION = "semantic-fact-v2"
LEGACY_PREDICTOR = "LEGACY_UNATTRIBUTED"


def strict_bool(data, key, default=False):
    value = data.get(key, default)
    if type(value) is not bool:
        raise ValueError("boolean_required:" + key)
    return value


def numeric_identity(value):
    # JSON integers and decimal strings only. JSON fractional source tokens are
    # decoded as strings, never binary floats. No context-dependent normalize().
    if type(value) not in (int, str):
        raise ValueError("decimal_string_or_integer_required")
    number = decimal(value)
    if not number:
        return [0, "0", 0]
    sign, digits, exponent = number.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return [sign, "".join(map(str, digits)), exponent]


def period_identity(data):
    return [stamp(data[k]) if data[k] is not None else None for k in ("period_start", "period_end")]


def observation_identity(data):
    value = data["value"]
    if data["value_type"] == "DECIMAL" and value is not None:
        value = numeric_identity(value)
    return [data["asset_id"], data["metric"], data["unit"], period_identity(data), data["value_type"], value]


def semantic_fact_id(data, snapshot):
    loc = data["locator"]
    return FACT_IDENTITY_VERSION + ":" + digest([
        observation_identity(data), snapshot["sha256"],
        {k: loc[k] for k in ("quote", "json_pointer") if k in loc}])


def predictor_id(forecast):
    if "policy_id" not in forecast:
        return LEGACY_PREDICTOR
    key = forecast["policy_id"]
    if not isinstance(key, str) or not key.strip() or key == LEGACY_PREDICTOR:
        raise ValueError("explicit_registered_policy_required")
    return key


def chain_key(forecast):
    return forecast["question_id"], predictor_id(forecast)
