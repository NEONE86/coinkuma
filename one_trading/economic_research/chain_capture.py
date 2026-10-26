"""Bounded read-only Ethereum JSON-RPC with PR8/v4 raw-byte and receipt storage."""
from __future__ import annotations

import json
import uuid
from urllib.error import HTTPError
from urllib.request import Request

from .evidence import VersionedEvidence, source_bytes, validate_url
from .store import atomic_bytes, canonical, digest, utc, write_json
from one_trading.value_evidence import sha

ENDPOINT = "https://ethereum-rpc.publicnode.com"
READ_METHODS = frozenset({"eth_chainId", "eth_getBlockByNumber", "eth_getBlockReceipts",
                          "eth_getCode", "eth_call"})
MAX_BYTES = 12 * 1024 * 1024


class CaptureError(ValueError):
    def __init__(self, message, receipt_id=None):
        super().__init__(message)
        self.receipt_id = receipt_id


class ChainCapture(VersionedEvidence):
    """No signing, accounts, transaction submission, subscriptions or infinite retry.

    HTTP and JSON-RPC error responses retain their exact bounded body. The request
    body is part of each journal receipt; its hash distinguishes POST requests at
    the same endpoint. Failed bodies are not usable source snapshots.
    """

    def rpc(self, method, params, endpoint=ENDPOINT):
        if endpoint != ENDPOINT or method not in READ_METHODS or not isinstance(params, list):
            raise ValueError("read_only_rpc_required")
        validate_url(endpoint)
        payload = dict(jsonrpc="2.0", id=digest([method, params])[:16], method=method, params=params)
        body = canonical(payload)
        receipt = dict(receipt_id=uuid.uuid4().hex, url=endpoint, requested_at=utc(),
                       source_kind="ETHEREUM_RPC", published_at=None, status=None,
                       request_method="POST", request_payload=payload, request_sha256=sha(body),
                       response_body_present=False, truncated=False)
        raw = None
        try:
            try:
                with self.opener.open(Request(endpoint, data=body, headers={
                        "User-Agent": "ONE-bounded-chain-research/1", "Content-Type": "application/json",
                        "Accept-Encoding": "identity"}), timeout=25) as response:
                    if response.url.rstrip("/") != endpoint:
                        raise ValueError("rpc_redirect_rejected")
                    receipt.update(status=response.status, final_url=response.url,
                                   headers={k: v for k, v in response.headers.items()
                                            if k.lower() in {"date", "content-type", "etag"}})
                    raw = response.read(MAX_BYTES + 1)
            except HTTPError as exc:
                raw = exc.read(MAX_BYTES + 1)
                receipt["status"] = exc.code
                raise
            if receipt["status"] != 200 or len(raw) > MAX_BYTES:
                raise ValueError("invalid_or_oversize_rpc_response")
            decoded = json.loads(raw)
            if not isinstance(decoded, dict) or decoded.get("jsonrpc") != "2.0" or decoded.get("id") != payload["id"]:
                raise ValueError("rpc_envelope_mismatch")
            if "error" in decoded or "result" not in decoded or decoded["result"] is None:
                raise ValueError("rpc_error_or_missing_result:" + str(decoded.get("error")))
        except Exception as exc:
            receipt["error"] = type(exc).__name__ + ": " + str(exc)
        receipt["received_at"] = utc()
        if raw is not None:
            receipt.update(response_body_present=True, sha256=sha(raw), size=len(raw),
                           path="raw/" + sha(raw) + ".bin", truncated=len(raw) > MAX_BYTES)
            path = self.root / receipt["path"]
            if path.exists() and path.read_bytes() != raw:
                raise ValueError("content_address_collision")
            if not path.exists():
                atomic_bytes(path, raw)
        self._receipt(receipt)
        self._index(receipt)
        write_json(self.root / "sources.json", self.sources)
        indexed = self.store.get("retrieval_receipt", receipt["receipt_id"])
        if receipt.get("error"):
            raise CaptureError(receipt["error"], receipt["receipt_id"])
        return decoded["result"], indexed


class ReplayCapture:
    """Replay only the explicit capture map, never a mutable latest-response cache."""

    def __init__(self, store, requests):
        self.store, self.requests = store, requests

    def rpc(self, method, params, endpoint=ENDPOINT):
        key = digest([method, params])
        row = self.requests[key]
        receipt = self.store.get("retrieval_receipt", row["receipt_id"])
        request = receipt["request_payload"]
        if request["method"] != method or request["params"] != params or receipt["url"] != endpoint:
            raise ValueError("replay_request_mismatch")
        if receipt.get("error"):
            raise CaptureError(receipt["error"], receipt["receipt_id"])
        raw = source_bytes(self.store, receipt["snapshot_id"])
        decoded = json.loads(raw)
        if decoded.get("id") != request["id"] or "error" in decoded:
            raise ValueError("replay_response_mismatch")
        return decoded["result"], receipt
