import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from one_trading.ai_native.live_model import ResponsesModel, ModelTransportError

class ModelErrorTests(unittest.TestCase):
    def model(self):
        return ResponsesModel(dict(model_calls_authorized=True,approval_reference="fixture",usd_usdt="1",
            rates={"input":".75","cached":".075","output":"4.5"},reserve_per_call_usdt=".05",
            max_output_tokens=1024,model="fixture",expires_at="2099-01-01T00:00:00+00:00"))
    def test_http_statuses_redaction_bounded_no_retry(self):
        for code in (400,401,403,404,429,500):
            with self.subTest(code=code), patch.dict("os.environ",{"OPENAI_API_KEY":"secret-fixture"}):
                error=urllib.error.HTTPError("https://api.openai.com/v1/responses",code,"private message",{
                    "x-request-id":"req-fixture","Authorization":"secret-fixture"},io.BytesIO(json.dumps({
                    "error":{"type":"invalid_request_error","code":"insufficient_quota","message":"secret-fixture user@example.org"}}).encode()))
                opener=MagicMock();opener.open.side_effect=error
                with patch("urllib.request.build_opener",return_value=opener):
                    with self.assertRaises(ModelTransportError) as caught:self.model().respond({})
                r=caught.exception.receipt
                self.assertEqual(r["http_status"],code)
                self.assertEqual(r["error_code"],"insufficient_quota")
                self.assertEqual(r["request_id"],"req-fixture")
                self.assertEqual(r["completion"],"REJECTED_HTTP" if code<500 else "UNKNOWN_COMPLETION")
                self.assertNotIn("secret-fixture",json.dumps(r));self.assertNotIn("user@",json.dumps(r))
                self.assertEqual(opener.open.call_count,1)
                self.assertEqual(opener.open.call_args.args[0].get_header("X-client-request-id"),r["client_request_id"])
                self.assertLessEqual(r["request_at"],r["response_at"])
    def test_timeout_and_corruption_unknown(self):
        for fault in (TimeoutError("secret-fixture"), b"broken-json", b'{"usage":{"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}},"output":[null]}'):
            with patch.dict("os.environ",{"OPENAI_API_KEY":"secret-fixture"}):
                opener=MagicMock()
                if isinstance(fault,Exception):opener.open.side_effect=fault
                else:
                    response=MagicMock();response.__enter__.return_value=response;response.status=200
                    response.headers={};response.read.return_value=fault;opener.open.return_value=response
                with patch("urllib.request.build_opener",return_value=opener):
                    with self.assertRaises(ModelTransportError) as caught:self.model().respond({})
                self.assertEqual(caught.exception.receipt["completion"],"UNKNOWN_COMPLETION")
                self.assertNotIn("secret-fixture",json.dumps(caught.exception.receipt))
                self.assertEqual(opener.open.call_count,1)
    def test_oversized_http_body_not_logged(self):
        with patch.dict("os.environ",{"OPENAI_API_KEY":"secret-fixture"}):
            e=urllib.error.HTTPError("https://api.openai.com",429,"",{},io.BytesIO(b"x"*100000))
            opener=MagicMock();opener.open.side_effect=e
            with patch("urllib.request.build_opener",return_value=opener):
                with self.assertRaises(ModelTransportError) as caught:self.model().respond({})
            self.assertLess(len(json.dumps(caught.exception.receipt)),1024)
            self.assertEqual(caught.exception.receipt["completion"],"UNKNOWN_COMPLETION")

    def test_transport_semantic_tamper_rejected(self):
        import tempfile
        from pathlib import Path
        from one_trading.ai_native.live_audit import verify_transport
        from one_trading.ai_native.audit import AuditError
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            row={"kind":"decision_requested","at":"2026-01-01T00:00:00+00:00","payload":{"request_id":"request-1"}}
            (root/"decisions.jsonl").write_text(json.dumps(row)+"\n")
            t=dict(client_request_id="00000000-0000-4000-8000-000000000001",request_at=row["at"],response_at=row["at"],
                   http_status=429,response_complete=True,completion="REJECTED_HTTP")
            f=root/"request-1-failure.json";f.write_text(json.dumps({"transport":t}))
            self.assertEqual(verify_transport(root)["rejected"],1)
            t["http_status"]=500;f.write_text(json.dumps({"transport":t}))
            with self.assertRaisesRegex(AuditError,"FALSE_HTTP"):verify_transport(root)
            t["completion"]="UNKNOWN_COMPLETION";f.write_text(json.dumps({"transport":t}))
            self.assertEqual(verify_transport(root)["unknown"],1)
            (root/"decisions.jsonl").write_text((json.dumps(row)+"\n")*2)
            with self.assertRaisesRegex(AuditError,"DUPLICATE"):verify_transport(root)

    def test_first_failure_stops_capture_without_waiting_duration(self):
        import tempfile
        from pathlib import Path
        from test_ai_native import packet
        from one_trading.ai_native.prospective_live import run
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d, patch("one_trading.ai_native.prospective_live.subprocess.check_output",side_effect=["a"*40,"",""]), \
             patch("one_trading.ai_native.prospective_live.EvidenceRecorder"), \
             patch("one_trading.ai_native.prospective_live.LivePlanner") as planner, \
             patch("one_trading.ai_native.prospective_live.Experiment") as engine, \
             patch("one_trading.ai_native.prospective_live.Tail") as tail, \
             patch("one_trading.ai_native.prospective_live.subprocess.Popen") as proc:
            planner.return_value.failed=True
            engine.return_value._frames=1
            engine.return_value.ledger.records=[]
            proc.return_value.poll.return_value=None
            proc.return_value.returncode=-1
            f=packet(0);seq={"BTCUSDT":1}
            raw=MagicMock();raw.read.return_value=[dict(seq=1,sha256="b"*64,received_at=f["received_at"])]
            events=MagicMock();events.read.return_value=[dict(kind="frame_provenance",frame_id=f["id"],source_seqs=seq,raw_cutoff_seq=1)]
            frames=MagicMock();frames.read.return_value=[f]
            tail.side_effect=[raw,events,frames]
            cfg=dict(duration_seconds=600,max_calls=10,decision_interval_seconds=30,max_bytes=100000000,
                     source_commit="a"*40,cost_limit_usdt="0")
            result=run(Path(d)/"new",cfg,enable_public_network=True)
            proc.return_value.terminate.assert_called_once()
            engine.return_value.observe.assert_called_once()
            self.assertEqual(result["status"],"INCOMPLETE")
            self.assertIn("MODEL_FAILURE_STOP_NO_RETRY",json.loads((Path(d)/"new/termination.json").read_text())["error"])
