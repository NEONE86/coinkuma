import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from one_trading.futures_evidence import gate, raw_oi_audit, source_bytes, ticker_audit


class EvidenceTests(unittest.TestCase):
    def test_carried_forward_OI_is_not_a_fresh_event(self):
        text='exchange,symbol,timestamp,local_timestamp,open_interest\n'
        text+='binance-futures,BTCUSDT,1640995200000000,1640995200000001,100\n'
        text+='binance-futures,BTCUSDT,1640995201000000,1640995201000001,100\n'
        r=ticker_audit(gzip.compress(text.encode()),'BTCUSDT','2022-01-01')
        self.assertEqual(r['repeated_oi_value_rows'],1)
        self.assertFalse(r['fresh_OI_event_timestamps_recoverable_from_csv_alone'])

    def test_raw_receipt_retains_observation_latency(self):
        payload=dict(stream='btcusdt@openInterest',generated=True,
            data=dict(symbol='BTCUSDT',openInterest='100',time=1640995200000))
        text='2022-01-01T00:00:02.1234567Z '+json.dumps(payload)+'\n'
        r=raw_oi_audit(gzip.compress(text.encode()),'BTCUSDT','2022-01-01',0)
        self.assertEqual(r['min_lag_us'],2_123_456)

    def test_raw_identity_cannot_cross_contracts(self):
        payload=dict(stream='ethusdt@openInterest',generated=True,
            data=dict(symbol='ETHUSDT',openInterest='100',time=1640995200000))
        with self.assertRaisesRegex(ValueError,'identity'):
            raw_oi_audit(gzip.compress(('2022-01-01T00:00:02.0000000Z '+json.dumps(payload)).encode()),'BTCUSDT','2022-01-01',0)

    def test_samples_cannot_unlock_performance(self):
        p=dict(evaluation=['2022-01-01','2026-10-01'],symbols=['BTCUSDT'])
        sample=[dict(symbol='BTCUSDT',day='2022-01-01')]
        result=gate(p,sample,[],[])
        self.assertFalse(result['data_gate_passed']);self.assertIsNone(result['performance_metrics'])
        self.assertIn('BTCUSDT:OI_CONTINUOUS_HISTORICAL_RECEIPT_INCOMPLETE',result['reasons'])

    def test_hash_tampering_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'http').mkdir();url='https://example.invalid/sample'
            (root/'http'/(hashlib.sha256(url.encode()).hexdigest()+'.bin')).write_bytes(b'altered')
            with self.assertRaisesRegex(ValueError,'integrity'):
                source_bytes(root,dict(url=url,sha256=hashlib.sha256(b'original').hexdigest(),bytes=7))

    def test_out_of_slice_receipt_rejected(self):
        payload=dict(stream='btcusdt@openInterest',generated=True,
            data=dict(symbol='BTCUSDT',openInterest='100',time=1640995200000))
        with self.assertRaisesRegex(ValueError,'slice'):
            raw_oi_audit(gzip.compress(('2022-01-02T00:00:02.0000000Z '+json.dumps(payload)).encode()),'BTCUSDT','2022-01-01',0)
