"""A frozen, finite Ethereum hook fee census; gross transfers are not causal lift."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from .chain_capture import ChainCapture, ReplayCapture, CaptureError, ENDPOINT
from .evidence import observe, validate_observation
from .events import register_event
from .store import digest, identifier, instant, utc
from .theses import make_packet, validate_packet

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FEE = "0xdb249cf7a102711dda1beaba415bb011ee0e1402c7f67617236fed5739235d6c"
SWAP = "0x6150bb53cd76c5702d6074239fc7b6c2a5495d339eb122fdc06252f3efdbbc1b"
RELEASED = "0x0143172ff1dd87f3691e870b3fb5616db820278d26d2d16c3e03330a240a6c38"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def number(value):
    require(isinstance(value, str) and re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value), "invalid_rpc_quantity")
    return int(value, 16)


def address(value):
    require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value), "invalid_chain_address")
    return value.lower()


def topic_address(value):
    require(isinstance(value, str) and re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", value), "invalid_address_topic")
    return address("0x" + value[-40:])


def uint256(value):
    require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value), "invalid_uint256_word")
    return int(value, 16)


def units(raw, decimals):
    require(type(raw) is int and raw >= 0 and type(decimals) is int and 0 <= decimals <= 36, "invalid_token_units")
    with localcontext() as ctx:
        ctx.prec = 120
        return format(Decimal(raw) / (Decimal(10) ** decimals), "f")


def freeze_spec(store, spec):
    require(spec["chain_id"] == 1 and type(spec["chain_id"]) is int, "ethereum_only")
    require(spec["endpoint"] == ENDPOINT, "endpoint_mismatch")
    start, end = spec["start_block"], spec["end_block"]
    require(type(start) is int and type(end) is int and 0 <= start <= end and end - start < 32, "bounded_range_required")
    require(instant(spec["end_utc"]) <= instant(spec["frozen_at"]) <= instant(utc()), "historical_freeze_time")
    for a in spec["addresses"].values():
        require(address(a) == a, "normalized_address_required")
    address(spec["uni_sink"])
    require(spec["uni_decimals"] == 18 and type(spec["uni_decimals"]) is int, "canonical_uni_units")
    require(set(spec["identity_proofs"]) == {"hook", "jar", "firepit", "uni", "adapter"}, "identity_proofs_required")
    for role, proof in spec["identity_proofs"].items():
        require(proof["address"] == spec["addresses"][role], "identity_proof_address_mismatch")
    require(spec["fee_topic"] == FEE and spec["swap_topic"] == SWAP and spec["transfer_topic"] == TRANSFER, "abi_topic_mismatch")
    return store.put("collection_spec", spec["spec_id"], spec)


def validate_page(block, receipts):
    """One block is one page; compare all transaction identities, not only count."""
    require(isinstance(receipts, list) and isinstance(block["transactions"], list), "missing_receipt_page")
    transactions = block["transactions"]
    require(all(isinstance(t, str) for t in transactions), "hash_only_block_required")
    require(len(set(transactions)) == len(transactions), "duplicate_block_transaction")
    require(len(receipts) == len(transactions), "incomplete_receipt_page")
    require({r["transactionHash"] for r in receipts} == set(transactions), "receipt_transaction_set_mismatch")
    indexes, log_indexes = set(), set()
    for r in receipts:
        ix = number(r["transactionIndex"])
        require(ix < len(transactions) and ix not in indexes and transactions[ix] == r["transactionHash"], "receipt_transaction_index")
        indexes.add(ix)
        require(r["blockHash"] == block["hash"] and r["blockNumber"] == block["number"], "receipt_block_mismatch")
        require(number(r["status"]) in (0, 1), "invalid_execution_status")
        require(isinstance(r["logs"], list), "missing_logs")
        require(number(r["status"]) == 1 or not r["logs"], "failed_transaction_has_logs")
        for log in r["logs"]:
            li = number(log["logIndex"])
            require(li not in log_indexes, "duplicate_log_index")
            log_indexes.add(li)
            require(log.get("removed") is False, "removed_or_unknown_log")
            require(log["blockHash"] == block["hash"] and log["blockNumber"] == block["number"]
                    and log["transactionHash"] == r["transactionHash"] and log["transactionIndex"] == r["transactionIndex"], "log_receipt_identity")
    require(log_indexes == set(range(len(log_indexes))), "noncontiguous_log_indexes")


def extract_flows(spec, block, receipts):
    """Exact ERC20 transfer matching; native and shared-jar amounts stay separate."""
    a = spec["addresses"]; hook, jar, manager = a["hook"], a["jar"], a["pool_manager"]
    found, swaps, shared, unresolved = [], [], [], []
    for ri, r in enumerate(receipts):
        if number(r["status"]) == 0:
            continue
        hook_swaps = [l for l in r["logs"] if address(l["address"]) == hook and l["topics"] and l["topics"][0] == SWAP]
        for l in hook_swaps:
            require(len(l["topics"]) == 3 and re.fullmatch(r"0x[0-9a-fA-F]{192}", l["data"]), "swap_abi_shape")
        swaps.extend(dict(transaction_hash=r["transactionHash"], block_hash=block["hash"], log_index=number(l["logIndex"])) for l in hook_swaps)
        used = set()
        for li, log in enumerate(r["logs"]):
            topics = log["topics"]
            if address(log["address"]) == hook and topics and topics[0] == FEE:
                require(len(topics) == 3, "fee_abi_shape")
                recipient, token, amount = topic_address(topics[1]), topic_address(topics[2]), uint256(log["data"])
                entry = dict(chain_id=1, block_number=number(block["number"]), block_hash=block["hash"],
                             transaction_hash=r["transactionHash"], log_index=number(log["logIndex"]),
                             token=token, raw_amount=str(amount), effective_at=datetime.fromtimestamp(number(block["timestamp"]), timezone.utc).isoformat(),
                             json_pointer=f"/result/{ri}/logs/{li}", fee_recipient=recipient)
                if not hook_swaps or recipient != jar or token == "0x" + "0" * 40:
                    unresolved.append(dict(entry, reason="no_swap_wrong_recipient_or_native_trace_required"));continue
                matches = []
                for candidate in r["logs"]:
                    ts = candidate["topics"]
                    if (address(candidate["address"]) == token and len(ts) == 3 and ts[0] == TRANSFER
                            and topic_address(ts[1]) == manager and topic_address(ts[2]) == jar
                            and uint256(candidate["data"]) == amount and number(candidate["logIndex"]) not in used):
                        matches.append(candidate)
                if not matches:
                    unresolved.append(dict(entry, reason="erc20_transfer_not_reconciled"));continue
                chosen = min(matches, key=lambda l: number(l["logIndex"]))
                used.add(number(chosen["logIndex"]))
                found.append(dict(entry, transfer_log_index=number(chosen["logIndex"]), attribution="DIRECT_HOOK_TO_SHARED_JAR"))
            if (address(log["address"]) == a["uni"] and len(topics) == 3 and topics[0] == TRANSFER
                    and topic_address(topics[2]) == spec["uni_sink"]):
                # A canonical sink transfer alone does not prove a Firepit release.
                shared.append(dict(transaction_hash=r["transactionHash"], block_hash=block["hash"],
                                   log_index=number(log["logIndex"]), raw_amount=str(uint256(log["data"])),
                                   firepit_release_log_present=any(address(l["address"]) == a["firepit"] and len(l["topics"]) == 3
                                       and l["topics"][0] == RELEASED for l in r["logs"]),
                                   attribution="UNATTRIBUTED_SHARED_JAR_CONTEXT", hook_attributed_amount=None))
    return found, swaps, shared, unresolved


def measure_legacy(store, spec, capture):
    requests, errors, pages = {}, [], []
    def call(method, params):
        key = digest([method, params])
        try:
            result, receipt = capture.rpc(method, params, spec["endpoint"])
        except CaptureError as exc:
            if exc.receipt_id:
                requests[key] = dict(receipt_id=exc.receipt_id)
            raise
        requests[key] = dict(receipt_id=receipt["receipt_id"], snapshot_id=receipt["snapshot_id"])
        return result, receipt
    try:
        require(number(call("eth_chainId", [])[0]) == spec["chain_id"], "chain_mismatch")
        finalized, _ = call("eth_getBlockByNumber", ["finalized", False])
        require(number(finalized["number"]) >= spec["end_block"], "not_finalized")
        for role, proof in spec["identity_proofs"].items():
            code, _ = call("eth_getCode", [proof["address"], hex(spec["end_block"])])
            import hashlib
            require(hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() == proof["deployed_code_sha256"], "contract_code_mismatch:" + role)
        a = spec["addresses"]
        for role, selector, expected in (("hook", "490c98f5", a["jar"]), ("hook", "dc4c90d3", a["pool_manager"]),
                ("adapter", "e0ef2b4b", a["jar"]), ("jar", "02691bcb", a["firepit"]),
                ("firepit", "2b3ed84e", a["uni"]), ("firepit", "ec0fa86d", spec["uni_sink"])):
            word, _ = call("eth_call", [dict(to=a[role], data="0x" + selector), hex(spec["end_block"])])
            require(topic_address(word) == expected, "route_binding_mismatch:" + role)
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(dict(stage="chain_identity_finality", error=str(exc)))
    flows, swaps, shared, unresolved, previous = [], [], [], [], None
    identity_verified = not errors
    for height in range(spec["start_block"], spec["end_block"] + 1):
        try:
            block, br = call("eth_getBlockByNumber", [hex(height), False])
            require(number(block["number"]) == height, "block_number_mismatch")
            if height == spec["start_block"]:
                require(block["hash"] == spec["start_hash"], "start_reorganized")
                require(number(block["timestamp"]) == int(instant(spec["start_utc"]).timestamp()), "start_time_mismatch")
            if height == spec["end_block"]:
                require(block["hash"] == spec["end_hash"], "end_reorganized")
                require(number(block["timestamp"]) == int(instant(spec["end_utc"]).timestamp()), "end_time_mismatch")
            if previous is not None:
                require(block["parentHash"] == previous, "broken_parent_chain")
            previous = block["hash"]
            receipts, rr = call("eth_getBlockReceipts", [hex(height)])
            validate_page(block, receipts)
            f, s, u, ambiguous = extract_flows(spec, block, receipts)
            for entry in f + s + u + ambiguous:
                entry.update(receipt_snapshot_id=rr["snapshot_id"], block_snapshot_id=br["snapshot_id"])
            flows.extend(f);swaps.extend(s);shared.extend(u);unresolved.extend(ambiguous)
            pages.append(dict(height=height, hash=block["hash"], parent_hash=block["parentHash"],
                              transactions=len(receipts), failed_transactions=sum(number(r["status"]) == 0 for r in receipts),
                              receipt_snapshot_id=rr["snapshot_id"], block_snapshot_id=br["snapshot_id"], status="COMPLETE"))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(dict(stage="block", height=height, error=str(exc)))
    # Same request key would erase a recheck response. Use full transactions for
    # the endpoint recheck so both requests remain distinct in the capture map.
    try:
        recheck, _ = call("eth_getBlockByNumber", [hex(spec["end_block"]), True])
        require(recheck["hash"] == spec["end_hash"], "end_reorganized_after_capture")
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(dict(stage="canonical_recheck", error=str(exc)))
    amounts, token_meta = defaultdict(int), {}
    for f in flows:
        amounts[f["token"]] += int(f["raw_amount"])
    for token in sorted(set(amounts) | {spec["addresses"]["uni"]}):
        try:
            word, receipt = call("eth_call", [dict(to=token, data="0x313ce567"), hex(spec["end_block"])])
            decimals = uint256(word); units(0, decimals)
            if token == spec["addresses"]["uni"]:
                require(decimals == spec["uni_decimals"], "canonical_uni_decimals_mismatch")
            token_meta[token] = dict(decimals=decimals, evidence_snapshot_id=receipt["snapshot_id"])
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(dict(stage="token_units", token=token, error=str(exc)))
    complete = not errors and len(pages) == spec["end_block"] - spec["start_block"] + 1
    return dict(spec_id=spec["spec_id"], spec_hash=digest(spec), status="COMPLETE" if complete else "DATA_MISSING",
                requests=requests, pages=pages, errors=errors, flows=flows, hook_swaps=swaps, identity_verified=identity_verified,
                unreconciled_fee_claims=unresolved, shared_uni_sink_transfers=shared, token_metadata=token_meta,
                totals=[dict(token=t, raw_amount=str(v), decimals=token_meta.get(t, {}).get("decimals"),
                             units=units(v, token_meta[t]["decimals"]) if t in token_meta else None) for t, v in sorted(amounts.items())],
                qualified_fee_count=len(flows) if complete and not unresolved else None,
                observed_qualified_fee_count=len(flows) if identity_verified else None,
                totals_scope="COMPLETE_RANGE" if complete else "PARTIAL_UNCONFIRMED_NOT_RANGE_TOTAL",
                zero_proven=complete and not flows and not unresolved,
                hook_attributed_uni=None, causal_increment=dict(status="AMBIGUOUS", value=None,
                    reason="Counterfactual replacement, incentives and conversion are not identified; gross transfers are not net increment."))


def measure(store, spec, capture):
    from .chain_qualification import qualify
    return qualify(store, spec, measure_legacy(store, spec, capture))


def validated_measurement(store, cycle):
    """Recheck raw inputs using the recorded measurement version; never rewrite it."""
    from .chain_qualification import VERSION, qualify
    old = cycle["measurement"]
    spec = store.get("collection_spec", old["spec_id"])
    # Canonical JSON sorts object keys in storage. Legacy preflight stopped at
    # its first failed code request, so recover the actual attempted role order
    # from receipts instead of interpreting sorted keys as execution order.
    code_attempts = {}
    for ref in old["requests"].values():
        receipt = store.get("retrieval_receipt", ref["receipt_id"])
        request = receipt["request_payload"]
        if request["method"] == "eth_getCode":
            code_attempts[request["params"][0]] = receipt["requested_at"]
    spec = dict(spec, identity_proofs=dict(sorted(spec["identity_proofs"].items(),
        key=lambda item: (code_attempts.get(item[1]["address"], "9999"), item[0]))))
    replay = measure_legacy(store, spec, ReplayCapture(store, old["requests"]))
    checked = qualify(store, spec, replay)
    require(old.get("measurement_version") in (None, VERSION), "unknown_measurement_version")
    require((checked if old.get("measurement_version") else replay) == old, "offline_replay_mismatch")
    return checked


def collect_cycle(store, spec, capture=None):
    freeze_spec(store, spec)
    key = identifier("chain-cycle", spec)
    old = store.get("collection_cycle", key, optional=True)
    if old:
        validated_measurement(store, old)
        return old
    started = utc()
    result = measure(store, spec, capture or ChainCapture(store))
    value = dict(cycle_id=key, started_at=started, completed_at=utc(), synthetic=False, measurement=result)
    return store.put("collection_cycle", key, value)


def validate_target(store, spec, asset_id):
    asset = store.get("asset_identity", asset_id)
    chain = asset["chain_id"]
    require((type(chain) is int and chain == spec["chain_id"])
            or chain == "eip155:" + str(spec["chain_id"]), "collection_asset_chain_mismatch")
    require(address(asset["contract_address"]) == address(spec["addresses"]["uni"])
            and asset["representation"] == "CANONICAL"
            and type(asset["decimals"]) is int and asset["decimals"] == spec["uni_decimals"],
            "collection_asset_identity_mismatch")


def validate_link(store, link, cycle, asset_id, measured):
    observation = store.get("observation", link["observation_id"])
    event = store.get("economic_event_revision", link["event_revision_id"])
    packet = store.get("evidence_packet", link["packet_id"])
    require(link["cycle_id"] == cycle["cycle_id"] and observation["collection_cycle_id"] == cycle["cycle_id"],
            "collection_link_cycle_mismatch")
    require(observation["asset_id"] == event["asset_id"] == asset_id
            and packet["subject_asset_ids"] == [asset_id], "collection_link_target_mismatch")
    require(observation["observation_id"] in event["observation_ids"]
            and observation["observation_id"] in packet["observation_ids"]
            and event["revision_id"] in packet["event_revision_ids"]
            and packet["manifest_hash"] == link["packet_hash"], "collection_link_reference_mismatch")
    require(event["event_id"] not in event["related_event_ids"], "collection_event_self_reference")
    require(event["status"] == measured["validation"]["event_status"], "collection_link_requires_review")
    validate_observation(store, observation)
    validate_packet(store, packet)
    from .chain_qualification import fact_key
    spec = store.get("collection_spec", cycle["measurement"]["spec_id"])
    for fact in measured["flows"]:
        if fact["qualification_status"] == "QUALIFIED":
            stored = store.get("chain_fee_fact", fact_key(spec, fact))
            require(all(stored.get(k) == v for k, v in fact.items() if k != "qualification_status"),
                    "collection_link_fact_mismatch")
    for review in store.all("collection_link_review"):
        require(event["revision_id"] not in review["excluded_event_revision_ids"], "quarantined_collection_event")
    return link


def link_cycle(store, cycle_id, asset_id):
    """Atomic domain link. Rejected identity inputs never cause domain writes.

    Old completed or interrupted rows are checked before reuse. Invalid legacy
    rows need an append-only review; this function never silently replaces them.
    """
    cycle = store.get("collection_cycle", cycle_id)
    spec = store.get("collection_spec", cycle["measurement"]["spec_id"])
    validate_target(store, spec, asset_id)  # including the existing-link fast path
    m = validated_measurement(store, cycle)
    existing = store.get("collection_link", cycle_id, optional=True)
    if existing:
        return validate_link(store, existing, cycle, asset_id, m)
    from .chain_qualification import fact_key, VERSION
    with store.transaction():
        sources = sorted({r["snapshot_id"] for r in m["requests"].values() if r.get("snapshot_id")})
        require(sources, "no_raw_source_to_link")
        oid = identifier("chain-observation", [cycle_id, asset_id])
        old_observation = store.get("observation", oid, optional=True)
        if old_observation:
            require(old_observation["asset_id"] == asset_id and old_observation["collection_cycle_id"] == cycle_id
                    and old_observation["evidence_ids"] == sources
                    and old_observation["metric"] == "bounded_hook_fee_collection_progress"
                    and old_observation["period_start"] == spec["start_utc"]
                    and old_observation["period_end"] == spec["end_utc"], "interrupted_observation_mismatch")
            require(not old_observation.get("qualification_version") or old_observation["qualification_version"] == VERSION,
                    "interrupted_observation_version")
            validate_observation(store, old_observation)
            observation = old_observation
        else:
            observation = observe(store, dict(observation_id=oid, asset_id=asset_id,
                metric="bounded_hook_fee_collection_progress",
                value="Collection " + m["status"] + "; qualified fee lower bound=" + str(m["observed_qualified_fee_count"]) + "; causal increment AMBIGUOUS",
                value_type="TEXT", unit="COLLECTION_PROGRESS", period_start=spec["start_utc"], period_end=spec["end_utc"],
                epistemic_type="SOURCE_CLAIM", evidence_ids=sources, locator=dict(snapshot_id=sources[0], json_pointer="/jsonrpc"),
                derivation_version=VERSION, qualification_version=VERSION, validation_hash=digest(m["validation"]),
                collection_cycle_id=cycle_id, missing_reason=None))
        event_id = identifier("collection-event", spec)
        related = [e for e in store.all("economic_event_revision") if e["asset_id"] == asset_id
                   and e["mechanism_id"] == "VALUE_CAPTURE_CHANGE" and e["event_id"] != event_id]
        cluster = related[0]["independent_cluster_id"] if related else "UNI-aggregator-hook-collection"
        for fact in m["flows"]:
            if fact["qualification_status"] != "QUALIFIED":
                continue
            fid = fact_key(spec, fact)
            old = store.get("chain_fee_fact", fid, optional=True)
            if old:
                require(all(old.get(k) == v for k, v in fact.items() if k != "qualification_status"),
                        "existing_fee_fact_conflict")
            else:
                store.put("chain_fee_fact", fid, dict(fact, fact_id=fid, first_cycle_id=cycle_id,
                    qualification_version=VERSION, synthetic=cycle["synthetic"], available_at=utc()))
        event = store.get("economic_event_revision", event_id + "-r1", optional=True)
        if event:
            require(event["asset_id"] == asset_id and event["observation_ids"] == [oid]
                    and event["mechanism_id"] == "VALUE_CAPTURE_CHANGE" and event["economic_flow_ids"] == []
                    and event["status"] == m["validation"]["event_status"]
                    and event_id not in event["related_event_ids"], "interrupted_event_requires_review")
        else:
            event = register_event(store, dict(event_id=event_id, revision_id=event_id + "-r1", asset_id=asset_id,
                mechanism_id="VALUE_CAPTURE_CHANGE", status=m["validation"]["event_status"],
                effective_at=spec["end_utc"], published_at=None, decision_authority="Frozen read-only collector; no trading authority",
                observation_ids=[oid], related_event_ids=sorted({e["event_id"] for e in related}), economic_flow_ids=[],
                independent_cluster_id=cluster, supersedes_revision_id=None))
        previous_packets = [p for p in store.all("evidence_packet") if p["subject_asset_ids"] == [asset_id]
                            and oid in p["observation_ids"] and event["revision_id"] in p["event_revision_ids"]]
        packet = validate_packet(store, previous_packets[0]) if previous_packets else make_packet(store, [asset_id])
        linked = dict(cycle_id=cycle_id, observation_id=oid, event_revision_id=event["revision_id"],
                      packet_id=packet["packet_id"], packet_hash=packet["manifest_hash"], available_at=utc())
        validate_link(store, linked, cycle, asset_id, m)
        return store.put("collection_link", cycle_id, linked)


def review_cycle(store, cycle_id):
    """Versioned offline reassessment; receipt clocks and old domain rows stay intact."""
    from .chain_qualification import VERSION
    cycle = store.get("collection_cycle", cycle_id)
    spec = store.get("collection_spec", cycle["measurement"]["spec_id"])
    m = validated_measurement(store, cycle)
    key = identifier("collection-review", [cycle_id, VERSION, digest(m)])
    old = store.get("collection_link_review", key, optional=True)
    if old:
        return old
    findings, excluded = [], []
    link = store.get("collection_link", cycle_id, optional=True)
    if link:
        event = store.get("economic_event_revision", link["event_revision_id"])
        try:
            validate_target(store, spec, event["asset_id"])
            validate_link(store, link, cycle, event["asset_id"], m)
        except ValueError as exc:
            findings.append(dict(record_kind="collection_link", record_id=cycle_id, reason=str(exc)))
            excluded.append(event["revision_id"])
    else:
        # Older non-atomic writers can leave an orphan event without a link.
        event = store.get("economic_event_revision", identifier("collection-event", spec) + "-r1", optional=True)
        if event:
            try:
                validate_target(store, spec, event["asset_id"])
                require(event["status"] == m["validation"]["event_status"], "orphan_event_requires_review")
            except ValueError as exc:
                findings.append(dict(record_kind="economic_event_revision", record_id=event["revision_id"], reason=str(exc)))
                excluded.append(event["revision_id"])
    rejected_facts = [f["fact_id"] for f in store.all("chain_fee_fact")
                      if f["first_cycle_id"] == cycle_id and f["fact_id"] not in m["validation"]["qualified_fact_ids"]]
    return store.put("collection_link_review", key, dict(review_id=key, cycle_id=cycle_id,
        validation_version=VERSION, original_measurement_version=cycle["measurement"].get("measurement_version", "legacy-v1"),
        cycle_hash=digest(cycle), validation=m["validation"], findings=findings,
        excluded_event_revision_ids=excluded, excluded_fee_fact_ids=rejected_facts,
        status="QUARANTINED_LINK" if excluded or rejected_facts else "VERIFIED_LINK",
        reassessed_at=utc(), available_at=utc(), no_new_network_requests=True))
