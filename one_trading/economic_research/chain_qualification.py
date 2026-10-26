"""Per-fact qualification, separate from completeness of the bounded census."""
from __future__ import annotations

import copy
from collections import defaultdict

from .chain_capture import ReplayCapture

VERSION = "chain-qualification-v2"


def fact_key(spec, row):
    return ":".join(map(str, (spec["chain_id"], row["block_hash"], row["transaction_hash"], row["log_index"])))


def qualify(store, spec, measured):
    """Called only after raw replay, never on unverified caller-supplied rows.

    A complete chain of captured headers from the fact to the rechecked anchor
    proves provider-consistent membership even if another receipt page is absent.
    A missing header in that path, failed route, reorg or unit failure blocks the
    fact. Identity/route/decimals use the frozen endpoint state, as in the spec;
    no claim of per-transaction state replay or consensus verification is made.
    """
    from .chain_cycle import number, units
    m = copy.deepcopy(measured)
    replay = ReplayCapture(store, m["requests"])
    def raw(method, params):
        try:
            return replay.rpc(method, params, spec["endpoint"])[0]
        except (ValueError, KeyError, TypeError):
            return None
    final = raw("eth_getBlockByNumber", ["finalized", False])
    anchor = raw("eth_getBlockByNumber", [hex(spec["end_block"]), True])
    finalized = final is not None and number(final["number"]) >= spec["end_block"]
    anchor_ok = anchor is not None and anchor["hash"] == spec["end_hash"]
    headers = {h: raw("eth_getBlockByNumber", [hex(h), False])
               for h in range(spec["start_block"], spec["end_block"] + 1)}
    fact_results, qualified, amounts = [], [], defaultdict(int)
    for flow in m["flows"]:
        height = flow["block_number"]
        path_ok = anchor_ok
        previous = flow["block_hash"]
        for h in range(height, spec["end_block"] + 1):
            b = headers[h]
            if b is None or (b["hash"] != previous if h == height else b["parentHash"] != previous):
                path_ok = False
                break
            previous = b["hash"]
        path_ok = path_ok and previous == spec["end_hash"]
        meta = m["token_metadata"].get(flow["token"])
        unit_ok = bool(meta and type(meta["decimals"]) is int and 0 <= meta["decimals"] <= 36)
        checks = dict(identity_and_route=m["identity_verified"], finalized=finalized,
                      canonical_membership=bool(path_ok), receipt_and_transfer=True, units=unit_ok)
        status = "QUALIFIED" if all(v is True for v in checks.values()) else "UNVERIFIED"
        fid = fact_key(spec, flow)
        fact_results.append(dict(fact_id=fid, status=status, checks=checks,
            failed_checks=sorted(k for k, v in checks.items() if v is not True),
            state_validation_block=spec["end_block"]))
        flow["qualification_status"] = status
        if status == "QUALIFIED":
            qualified.append(fid); amounts[flow["token"]] += int(flow["raw_amount"])
        else:
            flow["attribution"] = "UNVERIFIED_CANDIDATE"
    range_complete = (m["status"] == "COMPLETE" and len(qualified) == len(m["flows"])
                      and not m["unreconciled_fee_claims"])
    configured = m["identity_verified"] and finalized and anchor_ok and spec["addresses"]["uni"] in m["token_metadata"]
    validation = dict(version=VERSION, facts=fact_results, qualified_fact_ids=qualified,
        range_complete=range_complete, observed_qualified_fee_count=len(qualified),
        range_fee_count=len(qualified) if range_complete else None,
        zero_proven=range_complete and not qualified,
        event_status="EXECUTED" if qualified else "CONFIGURED" if configured else "PROPOSED")
    m.update(measurement_version=VERSION, validation=validation,
        qualified_fee_count=validation["range_fee_count"], observed_qualified_fee_count=len(qualified),
        zero_proven=validation["zero_proven"], status="COMPLETE" if range_complete else "DATA_MISSING",
        totals_scope="COMPLETE_RANGE" if range_complete else "OBSERVED_QUALIFIED_LOWER_BOUND",
        totals=[dict(token=t, raw_amount=str(v), decimals=m["token_metadata"][t]["decimals"],
                     units=units(v, m["token_metadata"][t]["decimals"])) for t, v in sorted(amounts.items())])
    return m
