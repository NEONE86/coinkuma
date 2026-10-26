"""New collector failure boundaries only; synthetic fixtures, not market evidence."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from one_trading.economic_research.chain_capture import ChainCapture, CaptureError, ENDPOINT
from one_trading.economic_research.chain_cycle import (FEE, SWAP, TRANSFER, collect_cycle,
    extract_flows, freeze_spec, measure, units, validate_page)
from one_trading.economic_research.store import Store, digest
from scripts.verify_collector_bundle import normalized_paths, verify


def addr(n): return "0x" + format(n, "040x")
def word(n): return "0x" + format(n, "064x")
def topic(a): return "0x" + "0" * 24 + a[2:]


def fixture():
    a = dict(hook=addr(1), jar=addr(2), firepit=addr(3), uni=addr(4), adapter=addr(5), pool_manager=addr(6))
    spec = dict(spec_id="synthetic-bounded", chain_id=1, endpoint=ENDPOINT, start_block=1, end_block=1,
        start_hash=word(1), end_hash=word(1), start_utc="2020-01-01T00:00:00Z", end_utc="2020-01-01T00:00:00Z",
        frozen_at="2020-01-02T00:00:00Z", addresses=a, uni_sink=addr(7), uni_decimals=18,
        identity_proofs={r: dict(address=a[r], deployed_code_sha256=hashlib.sha256(b"\x01").hexdigest())
                         for r in ("hook", "jar", "firepit", "uni", "adapter")},
        fee_topic=FEE, swap_topic=SWAP, transfer_topic=TRANSFER)
    block = dict(number="0x1", hash=word(1), parentHash=word(0), timestamp=hex(1577836800), transactions=[word(100)])
    receipt = dict(transactionHash=word(100), transactionIndex="0x0", blockHash=word(1), blockNumber="0x1", status="0x1", logs=[])
    for ix, (address, topics, data) in enumerate([
            (a["hook"], [SWAP, word(0), topic(addr(9))], "0x" + "0" * 192),
            (a["hook"], [FEE, topic(a["jar"]), topic(a["uni"])], word(10**18)),
            (a["uni"], [TRANSFER, topic(a["pool_manager"]), topic(a["jar"])], word(10**18))]):
        receipt["logs"].append(dict(address=address, topics=topics, data=data, logIndex=hex(ix), removed=False,
            transactionHash=word(100), transactionIndex="0x0", blockHash=word(1), blockNumber="0x1"))
    return spec, block, [receipt]


class Response:
    url = ENDPOINT
    status = 200
    headers = {}
    def __init__(self, raw): self.raw = raw
    def read(self, n): return self.raw[:n]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name); self.addCleanup(self.store.close)
        self.spec, self.block, self.receipts = fixture()

    def capture(self, mutation=None):
        capture = ChainCapture(self.store)
        def open_request(request, timeout):
            p = json.loads(request.data); method, params = p["method"], p["params"]
            if method == "eth_chainId": result = "0x1"
            elif method == "eth_getCode": result = "0x01"
            elif method == "eth_getBlockByNumber": result = copy.deepcopy(self.block)
            elif method == "eth_getBlockReceipts": result = copy.deepcopy(self.receipts)
            elif method == "eth_call":
                a = self.spec["addresses"]
                mapping = {"490c98f5": a["jar"], "dc4c90d3": a["pool_manager"], "e0ef2b4b": a["jar"],
                           "02691bcb": a["firepit"], "2b3ed84e": a["uni"], "ec0fa86d": self.spec["uni_sink"]}
                selector = params[0]["data"][2:]
                result = topic(mapping[selector]) if selector in mapping else word(18)
            else: raise AssertionError(method)
            if mutation: result = mutation(method, params, result)
            return Response(json.dumps(dict(jsonrpc="2.0", id=p["id"], result=result)).encode())
        capture.opener.open = open_request
        return capture

    def test_exact_fee_matching_and_replay_idempotency(self):
        first = collect_cycle(self.store, self.spec, self.capture())
        m = first["measurement"]
        self.assertEqual((m["status"], m["qualified_fee_count"], m["totals"][0]["units"]), ("COMPLETE", 1, "1"))
        count = self.store.verify()
        self.assertEqual(first, collect_cycle(self.store, self.spec))
        self.assertEqual(count, self.store.verify())
        self.assertIsNone(m["causal_increment"]["value"])

    def test_missing_receipt_cannot_prove_zero(self):
        self.receipts = []
        m = measure(self.store, self.spec, self.capture())
        self.assertEqual(m["status"], "DATA_MISSING"); self.assertFalse(m["zero_proven"])
        self.assertIsNone(m["qualified_fee_count"])

    def test_complete_no_log_range_proves_bounded_zero(self):
        self.receipts[0]["logs"] = []
        m = measure(self.store, self.spec, self.capture())
        self.assertEqual(m["qualified_fee_count"], 0); self.assertTrue(m["zero_proven"])

    def test_duplicate_or_removed_logs_rejected(self):
        for field, value in (("logIndex", "0x0"), ("removed", True), ("blockHash", word(9))):
            bad = copy.deepcopy(self.receipts); bad[0]["logs"][1][field] = value
            with self.assertRaises(ValueError): validate_page(self.block, bad)

    def test_failed_execution_is_not_counted(self):
        self.receipts[0]["status"] = "0x0"
        with self.assertRaisesRegex(ValueError, "failed_transaction"): validate_page(self.block, self.receipts)
        self.receipts[0]["logs"] = []
        m = measure(self.store, self.spec, self.capture())
        self.assertEqual(m["pages"][0]["failed_transactions"], 1)
        self.assertEqual(m["qualified_fee_count"], 0)

    def test_one_transfer_cannot_reconcile_two_fees(self):
        duplicate = copy.deepcopy(self.receipts[0]["logs"][1]); duplicate["logIndex"] = "0x3"
        self.receipts[0]["logs"].append(duplicate)
        m = measure(self.store, self.spec, self.capture())
        self.assertEqual(len(m["flows"]), 1); self.assertEqual(len(m["unreconciled_fee_claims"]), 1)
        self.assertIsNone(m["qualified_fee_count"]); self.assertFalse(m["zero_proven"])

    def test_reorg_and_unfinalized_cannot_prove_zero(self):
        for reorg in (True, False):
            def change(method, params, result):
                if method == "eth_getBlockByNumber" and (params[1] is True if reorg else params[0] == "finalized"):
                    result["hash" if reorg else "number"] = word(8) if reorg else "0x0"
                return result
            m = measure(self.store, self.spec, self.capture(change))
            self.assertEqual(m["status"], "DATA_MISSING"); self.assertFalse(m["zero_proven"])

    def test_identity_binding_and_unit_errors_fail_closed(self):
        for selector in ("0x313ce567", "0x490c98f5"):
            def change(method, params, result):
                return word(37) if method == "eth_call" and params[0]["data"] == selector else result
            m = measure(self.store, self.spec, self.capture(change))
            self.assertEqual(m["status"], "DATA_MISSING"); self.assertIsNone(m["qualified_fee_count"])
        self.assertEqual(units(10**18 + 1, 18), "1.000000000000000001")
        for value in (True, 1.0, -1):
            with self.assertRaises(ValueError): units(value, 18)

    def test_shared_uni_sink_is_never_attributed_to_hook(self):
        log = self.receipts[0]["logs"][2]; log["topics"][2] = topic(self.spec["uni_sink"])
        found, _, shared, unresolved = extract_flows(self.spec, self.block, self.receipts)
        self.assertEqual(found, []); self.assertEqual(len(unresolved), 1)
        self.assertIsNone(shared[0]["hook_attributed_amount"])
        self.assertFalse(shared[0]["firepit_release_log_present"])

    def test_http_error_bytes_and_rpc_failure_are_preserved(self):
        c = ChainCapture(self.store)
        raw = b'{"error":"rate limited"}'
        with patch.object(c.opener, "open", side_effect=HTTPError(ENDPOINT, 429, "limit", {}, io.BytesIO(raw))):
            with self.assertRaises(CaptureError): c.rpc("eth_chainId", [])
        r = self.store.all("retrieval_receipt")[-1]
        self.assertEqual((Path(self.temp.name) / "evidence" / r["path"]).read_bytes(), raw)
        self.assertIsNone(r["snapshot_id"])
        with patch.object(c.opener, "open", return_value=Response(b'{"jsonrpc":"2.0","id":1,"result":null}')):
            with self.assertRaises(CaptureError): c.rpc("eth_chainId", [])
        with self.assertRaisesRegex(ValueError, "read_only"): c.rpc("eth_sendRawTransaction", [])

    def test_spec_cannot_change_under_same_id(self):
        freeze_spec(self.store, self.spec)
        changed = copy.deepcopy(self.spec); changed["end_hash"] = word(9)
        with self.assertRaisesRegex(ValueError, "immutable_id_conflict"): freeze_spec(self.store, changed)

    def test_portable_manifest_safe_normalization_and_exact_bytes(self):
        root = Path(self.temp.name); (root / "nested").mkdir(); raw = b"original\r\nbytes\r\n"
        (root / "nested" / "raw.bin").write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        self.assertEqual(verify(root, {"nested\\raw.bin": expected})["files"], 1)
        for names in (("../outside",), ("/absolute",), ("C:\\drive",), ("a/../b",),
                      ("a\\b", "a/b"), ("A/b", "a/b"), ("a//b",), ("a:stream",)):
            with self.assertRaises(ValueError): normalized_paths(root, names)


if __name__ == "__main__": unittest.main()
