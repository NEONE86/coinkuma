"""Explicit contract identities, event revisions and separate supply ledgers."""
from __future__ import annotations

from decimal import Decimal, localcontext
import re

from .evidence import evidence_links, source_bytes, validate_observation
from .store import (check_revision, decimal, digest, instant, nonempty, one_of,
                    required, utc)

MECHANISMS = {"VALUE_CAPTURE_CHANGE", "NET_SUPPLY_CHANGE"}
STATUSES = {"PROPOSED", "AUTHORIZED", "CONFIGURED", "EXECUTED", "PAUSED", "RESUMED", "CANCELLED", "REVERSED"}


def register_asset(store, data):
    data = dict(data)
    nonempty(data, "asset_id", "symbol", "chain_id", "contract_address", "economic_asset_id",
             "exchange_symbol_mapping", "identity_evidence_ids")
    required(data, "decimals", "valid_from", "valid_to", "migration_evidence_ids", "representation")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", data["contract_address"]):
        raise ValueError("explicit_evm_contract_required")
    if type(data["decimals"]) is not int or not 0 <= data["decimals"] <= 36:
        raise ValueError("invalid_decimals")
    evidence_links(store, data["identity_evidence_ids"])
    if not any(data["contract_address"].lower().encode() in source_bytes(store, sid).lower()
               for sid in data["identity_evidence_ids"]):
        raise ValueError("contract_identity_not_in_evidence")
    instant(data["valid_from"])
    if data["valid_to"] and instant(data["valid_to"]) <= instant(data["valid_from"]):
        raise ValueError("invalid_identity_period")
    one_of(data["representation"], {"CANONICAL", "BRIDGED", "MIGRATED"})
    for venue, mapping in data["exchange_symbol_mapping"].items():
        nonempty(mapping, "instrument", "quote_currency", "evidence_ids", "mapping_basis")
        evidence_links(store, mapping["evidence_ids"])
    if data["representation"] != "CANONICAL":
        evidence_links(store, data["migration_evidence_ids"])
        underlying = store.get("asset_identity", data.get("underlying_asset_id"))
        if underlying["economic_asset_id"] != data["economic_asset_id"]:
            raise ValueError("underlying_economic_identity_mismatch")
    for prior in store.all("asset_identity"):
        if prior["asset_id"] == data["asset_id"]:
            continue
        same_contract = (prior["chain_id"], prior["contract_address"].lower()) == (data["chain_id"], data["contract_address"].lower())
        if same_contract:
            raise ValueError("duplicate_contract_identity")
        for venue, mapping in data["exchange_symbol_mapping"].items():
            old_mapping = prior["exchange_symbol_mapping"].get(venue)
            if old_mapping and mapping["instrument"] == old_mapping["instrument"]:
                nonoverlap = prior["valid_to"] and instant(prior["valid_to"]) <= instant(data["valid_from"])
                explicit_migration = (data["representation"] == "MIGRATED"
                                      and data.get("underlying_asset_id") == prior["asset_id"]
                                      and instant(data["valid_from"]) > instant(prior["valid_from"]))
                if not (nonoverlap or explicit_migration) or not data["migration_evidence_ids"]:
                    raise ValueError("ambiguous_symbol_mapping_or_migration")
                evidence_links(store, data["migration_evidence_ids"])
    old = store.get("asset_identity", data["asset_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    return store.put("asset_identity", data["asset_id"], data)


def register_flow(store, data):
    data = dict(data)
    nonempty(data, "economic_flow_id", "asset_id", "legs", "funding_source", "recipient",
             "costs", "decision_authority", "token_link", "redistribution_path")
    asset = store.get("asset_identity", data["asset_id"])
    seen = set()
    existing_refs = {leg["external_reference"] for flow in store.all("economic_flow")
                     if flow["economic_flow_id"] != data["economic_flow_id"] for leg in flow["legs"]}
    existing_legs = {leg["leg_id"] for flow in store.all("economic_flow")
                     if flow["economic_flow_id"] != data["economic_flow_id"] and flow["asset_id"] == data["asset_id"]
                     for leg in flow["legs"]}
    for leg in data["legs"]:
        nonempty(leg, "leg_id", "kind", "external_reference", "epistemic_type", "evidence_ids")
        required(leg, "quantity", "unit", "origin")
        if leg["leg_id"] in seen or leg["leg_id"] in existing_legs or leg["external_reference"] in existing_refs:
            raise ValueError("duplicate_economic_flow")
        seen.add(leg["leg_id"])
        existing_refs.add(leg["external_reference"])
        one_of(leg["kind"], {"ISSUANCE", "BURN", "BUYBACK", "UNLOCK", "REDISTRIBUTION", "RELOCK", "BRIDGE"})
        one_of(leg["epistemic_type"], {"DIRECT_RECORD", "SOURCE_CLAIM", "ASSUMPTION", "DERIVED_VALUE"})
        evidence_links(store, leg["evidence_ids"])
        if leg["unit"] != data["asset_id"]:
            raise ValueError("flow_unit_identity_mismatch")
        if leg["quantity"] is not None and decimal(leg["quantity"]) < 0:
            raise ValueError("negative_quantity")
        if leg["quantity"] is None and not leg.get("missing_reason"):
            raise ValueError("missing_flow_reason")
        if leg["kind"] == "ISSUANCE" and leg["origin"] in {"UNLOCK", "BRIDGE", "MIGRATION"}:
            raise ValueError("unlock_or_bridge_is_not_issuance")
        if asset["representation"] == "BRIDGED" and leg["kind"] == "ISSUANCE":
            raise ValueError("bridged_supply_is_not_global_issuance")
        if leg["kind"] == "BURN" and not leg.get("permanence_basis"):
            raise ValueError("burn_permanence_basis_required")
        if leg["kind"] == "BUYBACK" and leg["origin"] != "MARKET_PURCHASE":
            raise ValueError("inventory_burn_is_not_market_purchase")
    old = store.get("economic_flow", data["economic_flow_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    return store.put("economic_flow", data["economic_flow_id"], data)


def supply_summary(flows):
    """No aggregate 'buyback plus burn benefit'. Unknown amounts stay unknown.

    This is an evidence-labeled ledger, not a claim that source amounts are true.
    Burn permanence remains separately attributed to its stated evidence basis.
    """
    seen_flows, seen_refs = set(), set()
    if len({flow["asset_id"] for flow in flows}) > 1:
        raise ValueError("mixed_asset_supply_ledgers")
    totals = {kind: None for kind in ("ISSUANCE", "BURN", "BUYBACK", "UNLOCK", "REDISTRIBUTION", "RELOCK", "BRIDGE")}
    present = set()
    types = set()
    with localcontext() as context:
        context.prec = 80
        for flow in flows:
            if flow["economic_flow_id"] in seen_flows:
                raise ValueError("duplicate_economic_flow")
            seen_flows.add(flow["economic_flow_id"])
            for leg in flow["legs"]:
                if leg["external_reference"] in seen_refs:
                    raise ValueError("duplicate_flow_leg")
                seen_refs.add(leg["external_reference"])
                kind = leg["kind"]
                types.add(leg["epistemic_type"])
                if kind not in present:
                    totals[kind] = Decimal(0)
                    present.add(kind)
                if leg["quantity"] is None:
                    totals[kind] = None
                elif totals[kind] is not None:
                    totals[kind] += decimal(leg["quantity"])
        net = None if totals["ISSUANCE"] is None or totals["BURN"] is None else totals["ISSUANCE"] - totals["BURN"]
        trade_keys = ("UNLOCK", "REDISTRIBUTION", "RELOCK", "BUYBACK")
        trade = None if any(totals[k] is None for k in trade_keys) else totals["UNLOCK"] + totals["REDISTRIBUTION"] - totals["RELOCK"] - totals["BUYBACK"]
        return dict(gross={k: str(v) if v is not None else None for k, v in totals.items()},
                    net_economic_supply_change=str(net) if net is not None else None,
                    modeled_tradable_change=str(trade) if trade is not None else None,
                    epistemic_types=sorted(types), not_observed_sale_pressure=True,
                    scope="Only explicitly linked legs; omitted categories are null, never measured zeros.")


def register_event(store, data):
    data = dict(data)
    nonempty(data, "event_id", "revision_id", "asset_id", "mechanism_id", "status",
             "decision_authority", "observation_ids", "independent_cluster_id")
    required(data, "effective_at", "published_at", "related_event_ids", "economic_flow_ids", "supersedes_revision_id")
    store.get("asset_identity", data["asset_id"])
    one_of(data["mechanism_id"], MECHANISMS)
    one_of(data["status"], STATUSES)
    observations = [store.get("observation", oid) for oid in data["observation_ids"]]
    if len(set(data["observation_ids"])) != len(observations) or any(o["asset_id"] != data["asset_id"] for o in observations):
        raise ValueError("event_observation_identity_mismatch")
    for eid in data["related_event_ids"]:
        if not any(r["event_id"] == eid for r in store.all("economic_event_revision")):
            raise ValueError("missing_related_event")
    for fid in data["economic_flow_ids"]:
        if store.get("economic_flow", fid)["asset_id"] != data["asset_id"]:
            raise ValueError("event_flow_identity_mismatch")
    for key in ("effective_at", "published_at"):
        if data[key]:
            instant(data[key])
    check_revision(store, "economic_event_revision", data, "revision_id", "supersedes_revision_id",
                   ("event_id", "asset_id", "mechanism_id", "independent_cluster_id"))
    if data["supersedes_revision_id"]:
        previous = store.get("economic_event_revision", data["supersedes_revision_id"])
        transitions = {
            "PROPOSED": {"PROPOSED", "AUTHORIZED", "CONFIGURED", "EXECUTED", "CANCELLED"},
            "AUTHORIZED": {"AUTHORIZED", "CONFIGURED", "EXECUTED", "PAUSED", "CANCELLED", "REVERSED"},
            "CONFIGURED": {"CONFIGURED", "EXECUTED", "PAUSED", "CANCELLED", "REVERSED"},
            "EXECUTED": {"EXECUTED", "PAUSED", "REVERSED"},
            "PAUSED": {"PAUSED", "RESUMED", "CANCELLED", "REVERSED"},
            "RESUMED": {"RESUMED", "EXECUTED", "PAUSED", "REVERSED"},
            "CANCELLED": {"CANCELLED"}, "REVERSED": {"REVERSED"},
        }
        if data["status"] not in transitions[previous["status"]]:
            raise ValueError("invalid_event_state_transition")
    # Same source claims under a new event id do not become an independent event.
    facts = {f for o in observations for f in validate_observation(store, o)["fact_ids"]}
    for prior in store.all("economic_event_revision"):
        prior_facts = {f for oid in prior["observation_ids"]
                       for f in validate_observation(store, store.get("observation", oid))["fact_ids"]}
        if (prior["event_id"] != data["event_id"] and prior["asset_id"] == data["asset_id"]
                and prior["mechanism_id"] == data["mechanism_id"]
                and prior_facts == facts):
            raise ValueError("duplicate_economic_event")
        if (prior["event_id"] != data["event_id"] and prior_facts & facts
                and prior["independent_cluster_id"] != data["independent_cluster_id"]):
            raise ValueError("shared_facts_cannot_create_independent_cluster")
    old = store.get("economic_event_revision", data["revision_id"], optional=True)
    data["registered_at"] = old["registered_at"] if old else utc()
    data["available_at"] = data["registered_at"]
    data["observed_at"] = max(o["available_at"] for o in observations)
    return store.put("economic_event_revision", data["revision_id"], data)
