"""PR8 evidence-store extension: refresh, version index, pre-request redirects.

    Source text (including instructions in it) is untrusted data. It is never
    evaluated or passed to a shell. Capturing is read-only, bounded HTTPS GET.
"""
from __future__ import annotations

import json
from pathlib import Path
import uuid
from datetime import datetime, timezone
from decimal import localcontext
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from one_trading.value_evidence import EvidenceStore, HOSTS, sha
from .locations import locate
from .semantics import (FACT_IDENTITY_VERSION, numeric_identity, observation_identity,
                        period_identity, semantic_fact_id)
from .store import (atomic_bytes, canonical, decimal, digest, identifier, instant,
                    nonempty, one_of, required, stamp, utc, write_json)

REVIEWED_HOSTS = frozenset(HOSTS | {
    "aave.com", "docs.pancakeswap.finance", "blog.pancakeswap.finance",
    "pancakeswap.finance", "data-api.binance.vision",
    "tokens.uniswap.org", "raw.githubusercontent.com",
})
PRICE_PATHS = {"/api/v3/ticker/24hr", "/api/v3/exchangeInfo", "/api/v3/klines", "/api/v3/time"}


def validate_url(url):
    p = urlparse(url)
    if (p.scheme != "https" or p.hostname not in REVIEWED_HOSTS or p.username
            or p.password or p.port not in (None, 443) or p.fragment
            or any(ord(c) < 33 for c in url)):
        raise ValueError("source_not_allowlisted")
    if p.hostname == "data-api.binance.vision" and p.path not in PRICE_PATHS:
        raise ValueError("market_endpoint_not_allowlisted")
    return url


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)  # before urllib can issue the redirected request
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class VersionedEvidence(EvidenceStore):
    """Reuse PR8 content-addressing, receipt serialization and JSON decoder.

    A durable per-request journal is authoritative. The legacy requests.jsonl
    is also written using EvidenceStore._receipt for interoperability. A crash
    before SQLite/index update is recovered from the journal, not re-timestamped.
    One capture writer per workspace; SQLite handles registration serialization.
    """

    def __init__(self, store, offline=False):
        self.store = store
        self.root = store.root / "evidence"
        self.offline = offline
        self.sources = {}
        for name in ("raw", "receipts"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.opener = build_opener(CheckedRedirect())
        self.recover()

    def _receipt(self, record):
        write_json(self.root / "receipts" / (record["receipt_id"] + ".json"), record)
        super()._receipt(record)

    def recover(self):
        receipts = [json.loads(p.read_text(encoding="utf-8"))
                    for p in (self.root / "receipts").glob("*.json")]
        for r in sorted(receipts, key=lambda r: (r["received_at"], r["receipt_id"])):
            self._index(r)
        write_json(self.root / "sources.json", self.sources)

    def _index(self, receipt):
        r = dict(receipt)
        validate_url(r["url"])
        if not r.get("error"):
            path = self.root / r["path"]
            if path.resolve().parent != (self.root / "raw").resolve() or sha(path.read_bytes()) != r["sha256"]:
                raise ValueError("raw_tampered")
            sid = identifier("source", [r["url"], r["sha256"]])
            old = self.store.get("source_snapshot", sid, optional=True)
            previous = self.sources.get(r["url"])
            if old is None:
                snapshot = dict(snapshot_id=sid, source_url=r["url"], source_kind=r["source_kind"],
                                sha256=r["sha256"], raw_path=r["path"],
                                first_observed_at=r["received_at"], available_at=r["received_at"],
                                published_at=r.get("published_at"),
                                supersedes_snapshot_id=previous["snapshot_id"] if previous else None,
                                trust="UNTRUSTED_SOURCE_DATA")
                self.store.put("source_snapshot", sid, snapshot)
                old = snapshot
            r["snapshot_id"] = sid
            self.sources[r["url"]] = dict(r, first_seen_at=old["first_observed_at"])
        else:
            r["snapshot_id"] = None
        self.store.put("retrieval_receipt", r["receipt_id"], r)

    def get(self, url, refresh=True, source_kind="OFFICIAL_DOCUMENT", published_at=None):
        validate_url(url)
        if self.offline or not refresh:
            cached = self.sources.get(url)
            if not cached:
                raise ValueError("uncached_source_in_offline_mode")
            body = (self.root / cached["path"]).read_bytes()
            if sha(body) != cached["sha256"]:
                raise ValueError("raw_tampered")
            return body, cached
        receipt = dict(receipt_id=uuid.uuid4().hex, url=url, requested_at=utc(),
                       source_kind=source_kind, published_at=published_at, status=None)
        try:
            with self.opener.open(Request(url, headers={"User-Agent": "ONE-economic-research/4.0",
                                                       "Accept-Encoding": "identity"}), timeout=25) as response:
                validate_url(response.url)
                body = response.read(12 * 1024 * 1024 + 1)
                if response.status != 200 or len(body) > 12 * 1024 * 1024:
                    raise ValueError("invalid_or_oversize_response")
                receipt.update(status=response.status, final_url=response.url,
                               headers={k: v for k, v in response.headers.items()
                                        if k.lower() in {"date", "etag", "last-modified", "content-type"}})
            receipt.update(sha256=sha(body), size=len(body), path="raw/" + sha(body) + ".bin")
            raw = self.root / receipt["path"]
            if raw.exists() and raw.read_bytes() != body:
                raise ValueError("content_address_collision")
            if not raw.exists():
                atomic_bytes(raw, body)
            receipt["received_at"] = utc()  # never source publication or effective date
        except Exception as exc:
            receipt.update(received_at=utc(), error=type(exc).__name__ + ": " + str(exc),
                           status=getattr(exc, "code", receipt["status"]))
            self._receipt(receipt)
            self._index(receipt)
            raise
        self._receipt(receipt)  # journal before recoverable database/index writes
        self._index(receipt)
        write_json(self.root / "sources.json", self.sources)
        return body, self.sources[url]


def source_bytes(store, sid):
    snapshot = store.get("source_snapshot", sid)
    path = (store.root / "evidence" / snapshot["raw_path"]).resolve()
    if path.parent != (store.root / "evidence" / "raw").resolve():
        raise ValueError("invalid_raw_path")
    content = path.read_bytes()
    if sha(content) != snapshot["sha256"]:
        raise ValueError("raw_tampered")
    return content


def evidence_links(store, ids, cutoff=None):
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("missing_or_duplicate_evidence_links")
    for sid in ids:
        snapshot = store.get("source_snapshot", sid)
        validate_url(snapshot["source_url"])
        source_bytes(store, sid)
        if cutoff and instant(snapshot["available_at"]) > instant(cutoff):
            raise ValueError("future_evidence")


def validate_observation(store, data, cutoff=None, stack=()):
    """Recompute provenance from raw bytes and every input; never trust cached tags.

    Numeric semantics are explicit. TEXT claims can paraphrase a source, but can
    never be a numeric outcome. Period labels still require analyst/source review.
    """
    nonempty(data, "observation_id", "asset_id", "metric", "unit", "locator")
    required(data, "value", "value_type", "period_start", "period_end", "derivation_version", "evidence_ids", "epistemic_type")
    oid = data["observation_id"]
    if oid in stack:
        raise ValueError("derived_dependency_cycle")
    one_of(data["value_type"], {"DECIMAL", "TEXT", "BOOLEAN"})
    store.get("asset_identity", data["asset_id"])
    one_of(data["epistemic_type"], {"DIRECT_RECORD", "SOURCE_CLAIM", "DERIVED_VALUE", "ASSUMPTION"})
    available = data.get("available_at", cutoff or utc())
    if cutoff and instant(available) > instant(cutoff):
        raise ValueError("future_observation")
    evidence_links(store, data["evidence_ids"], available)
    # Locator is an exact UTF-8 excerpt or a JSON pointer, tied to one source.
    loc = data["locator"]
    nonempty(loc, "snapshot_id")
    if loc["snapshot_id"] not in data["evidence_ids"]:
        raise ValueError("locator_not_linked")
    raw = source_bytes(store, loc["snapshot_id"])
    located_value, raw_location = locate(raw, loc, numeric=data["value_type"] == "DECIMAL"
                                        and data["value"] is not None and data["epistemic_type"] != "DERIVED_VALUE")
    if data["value"] is None and not data.get("missing_reason"):
        raise ValueError("missing_reason_required")
    if data["value_type"] == "DECIMAL" and data["value"] is not None:
        numeric_identity(data["value"])
        if data["epistemic_type"] in {"DIRECT_RECORD", "SOURCE_CLAIM"} and decimal(located_value) != decimal(data["value"]):
            raise ValueError("numeric_observation_source_mismatch")
    elif data["value"] is not None:
        expected = str if data["value_type"] == "TEXT" else bool
        if type(data["value"]) is not expected:
            raise ValueError("observation_value_type_mismatch")
        if data["epistemic_type"] == "DIRECT_RECORD" and data["value"] != located_value:
            raise ValueError("direct_observation_source_mismatch")
    leaf_types = {data["epistemic_type"]}
    snapshot = store.get("source_snapshot", loc["snapshot_id"])
    # Same raw fact under an alias/URL reimport does not become a new fact.
    locations = [["text_span", *span] for span in raw_location[1]] if raw_location[0] == "text_spans" else [raw_location]
    fact_ids = {semantic_fact_id(data, snapshot, position) for position in locations}
    # Additional citations cannot make an already available scalar look new.
    source_available = [min((s["available_at"] for s in store.all("source_snapshot")
                             if s["sha256"] == snapshot["sha256"]), key=instant)]
    if data["epistemic_type"] == "DERIVED_VALUE":
        if data["value_type"] != "DECIMAL":
            raise ValueError("derived_value_must_be_numeric")
        nonempty(data, "input_observation_ids", "calculation", "derivation_version")
        if len(set(data["input_observation_ids"])) != len(data["input_observation_ids"]):
            raise ValueError("duplicate_derived_input")
        inputs = [store.get("observation", oid) for oid in data["input_observation_ids"]]
        if any(o["asset_id"] != data["asset_id"] or o["unit"] != data["unit"] or o.get("value_type") != "DECIMAL" for o in inputs):
            raise ValueError("derived_input_identity_or_unit")
        if any(period_identity(o) != period_identity(data) for o in inputs):
            raise ValueError("derived_input_period_mismatch")
        provenance = [validate_observation(store, o, available, (*stack, oid)) for o in inputs]
        leaf_types = {t for p in provenance for t in p["leaf_epistemic_types"]}
        fact_ids = {f for p in provenance for f in p["fact_ids"]}
        if sum(len(p["fact_ids"]) for p in provenance) != len(fact_ids):
            raise ValueError("duplicate_derived_fact")
        if not {sid for o in inputs for sid in o["evidence_ids"]}.issubset(data["evidence_ids"]):
            raise ValueError("derived_evidence_missing")
        one_of(data["calculation"], {"SUM", "SUBTRACT"})
        if data["calculation"] == "SUBTRACT" and len(inputs) != 2:
            raise ValueError("subtraction_requires_two_inputs")
        with localcontext() as context:
            context.prec = 80
            if any(o["value"] is None for o in inputs):
                computed = None
            elif data["calculation"] == "SUM":
                computed = sum(decimal(o["value"]) for o in inputs)
            else:
                computed = decimal(inputs[0]["value"]) - decimal(inputs[1]["value"])
            if (computed is None) != (data["value"] is None) or (computed is not None and computed != decimal(data["value"])):
                raise ValueError("derived_arithmetic_mismatch")
        source_available = [p["source_available_at"] for p in provenance]
    if data["period_start"] and data["period_end"] and instant(data["period_start"]) > instant(data["period_end"]):
        raise ValueError("invalid_period")
    if data["epistemic_type"] == "DIRECT_RECORD" and data["period_end"] and instant(data["period_end"]) > instant(utc()):
        raise ValueError("direct_measurement_cannot_end_in_future")
    return dict(fact_identity_version=FACT_IDENTITY_VERSION, raw_location=raw_location,
                leaf_epistemic_types=sorted(leaf_types), fact_ids=sorted(fact_ids),
                source_available_at=max(source_available, key=instant))


def observe(store, data):
    data = dict(data)
    old = store.get("observation", data["observation_id"], optional=True)
    data["available_at"] = old["available_at"] if old else utc()
    provenance = validate_observation(store, data)
    # Aliases retain the first parsing time; reparsing cannot launder old facts.
    aliases = []
    for o in store.all("observation"):
        if o["observation_id"] == data["observation_id"] or observation_identity(o) != observation_identity(data):
            continue
        try:
            same = validate_observation(store, o)["fact_ids"] == provenance["fact_ids"]
        except ValueError:
            # An invalid legacy locator cannot veto a valid correction. Retain
            # its earlier availability conservatively when value + raw agree;
            # it remains an audit issue and cannot become a derived input.
            prior_source = store.get("source_snapshot", o["locator"]["snapshot_id"])
            new_source = store.get("source_snapshot", data["locator"]["snapshot_id"])
            same = prior_source["sha256"] == new_source["sha256"]
        if same:
            aliases.append(o)
    if aliases and not old:
        data["available_at"] = min([data["available_at"], *(o["available_at"] for o in aliases)], key=instant)
    if data["epistemic_type"] == "DERIVED_VALUE":
        data["input_epistemic_types"] = sorted({store.get("observation", oid)["epistemic_type"] for oid in data["input_observation_ids"]})
    return store.put("observation", data["observation_id"], data)


def price_from_ticker(store, asset_id, sid):
    asset = store.get("asset_identity", asset_id)
    snapshot = store.get("source_snapshot", sid)
    if urlparse(snapshot["source_url"]).hostname != "data-api.binance.vision":
        raise ValueError("price_source_mismatch")
    ticker = json.loads(source_bytes(store, sid))
    mapping = asset["exchange_symbol_mapping"]["BINANCE"]
    if ticker["symbol"] != mapping["instrument"] or decimal(ticker["lastPrice"]) <= 0:
        raise ValueError("price_asset_mismatch")
    source_time = datetime.fromtimestamp(int(ticker["closeTime"]) / 1000, timezone.utc).isoformat()
    if instant(source_time) > instant(utc()):
        raise ValueError("future_price_source_time")
    if instant(source_time) < instant(asset["valid_from"]) or (asset["valid_to"] and instant(source_time) >= instant(asset["valid_to"])):
        raise ValueError("price_outside_asset_validity")
    for migrated in store.all("asset_identity"):
        if (migrated.get("representation") == "MIGRATED" and migrated.get("underlying_asset_id") == asset_id
                and instant(source_time) >= instant(migrated["valid_from"])):
            raise ValueError("price_mapping_superseded_by_migration")
    key = identifier("price", [asset_id, sid])
    old = store.get("price_snapshot", key, optional=True)
    price = dict(price_snapshot_id=key, asset_id=asset_id, venue="BINANCE",
                 instrument=mapping["instrument"], quote_currency=mapping["quote_currency"],
                 price_kind="LAST_TRADE_24HR_TICKER", value=ticker["lastPrice"],
                 source_time=source_time, observed_at=snapshot["first_observed_at"],
                 available_at=old["available_at"] if old else utc(), evidence_ids=[sid])
    return store.put("price_snapshot", key, price)
