"""Real-smoke failure regressions. Only fake transports, no network calls."""
import asyncio,io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from urllib.error import HTTPError
from one_sentinel.engine import Observer
from one_sentinel.transport import public_get,validate_approval,smoke
from one_sentinel.store import raw_usage
from one_sentinel.demo import T,metadata,mini
from test_sentinel_transport import Fake

class IngressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.o=Observer(self.root/'r');c=self.o.l.get('contract','v1')
        self.a={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        self.a.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_NO_ACTUAL_NETWORK',public_data=True,start_at=T-1,stop_at=T+1000,model_cash_budget=0,live_orders=False)
        validate_approval(self.o,self.a,T)
    def tearDown(self):self.o.close();self.tmp.cleanup()
    def test_unused_permission_sets_omitted_without_symbol_filter(self):
        from urllib.parse import urlsplit,parse_qs
        params=parse_qs(urlsplit(self.o.policy['sources']['metadata']['url']).query)
        self.assertEqual(params,{'showPermissionSets':['false']})
    def test_failed_large_body_counts_and_blocks_following_io(self):
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=20
        fake=Fake(b'x'*21)
        with self.assertRaisesRegex(ValueError,'TRUNCATED'):public_get(self.o,'metadata',clock=lambda:T,opener=fake)
        r=self.o.l.all('http_result')[0];self.assertEqual((r['bytes'],r['http_status'],r['truncated']),(20,200,True))
        self.assertEqual(raw_usage(self.o.l,T),20)
        with self.assertRaisesRegex(ValueError,'RAW_DAILY_LIMIT'):public_get(self.o,'metadata',clock=lambda:T,opener=fake)
        self.assertEqual(fake.calls,1);self.assertEqual(sum(p.stat().st_size for p in (self.o.l.root/'raw').glob('*.bin')),20)
    def test_failed_http_bytes_share_ws_budget(self):
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=20
        with self.assertRaises(ValueError):public_get(self.o,'metadata',clock=lambda:T,opener=Fake(b'x'*20))
        with self.assertRaisesRegex(ValueError,'RAW_DAILY_LIMIT'):self.o.ingest('mini',b'[]',T,T,'REAL_FORWARD')
    def test_pending_read_reservation_blocks_other_io_after_restart(self):
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=20
        with self.o.l.transaction():self.o.l.add('http_intent','crashed',dict(id='crashed',at=T,source='metadata',read_reservation=20))
        fake=Fake()
        with self.assertRaisesRegex(ValueError,'RAW_DAILY_LIMIT'):public_get(self.o,'metadata',clock=lambda:T,opener=fake)
        self.assertEqual(fake.calls,0)
    def test_success_is_not_double_counted(self):
        data=json.dumps(metadata()).encode();public_get(self.o,'metadata',clock=lambda:T,opener=Fake(data))
        self.assertEqual(raw_usage(self.o.l,T),len(data));r=self.o.l.all('http_result')[0];self.assertIsNotNone(r['input_receipt_id'])
    def test_http_error_body_and_status_preserved(self):
        class Reject:
            def open(self,*a,**kw):raise HTTPError('https://example.invalid',429,'slow down',{},io.BytesIO(b'{"code":-1003}'))
        with self.assertRaisesRegex(ValueError,'HTTP_STATUS'):public_get(self.o,'metadata',clock=lambda:T,opener=Reject())
        r=self.o.l.all('http_result')[0];self.assertEqual(r['http_status'],429);self.assertEqual(r['bytes'],14);self.assertFalse(r['truncated'])
        self.assertEqual((self.o.l.root/'raw'/(r['raw_hash']+'.bin')).read_bytes(),b'{"code":-1003}')
    def test_exact_read_boundary_is_conservatively_truncated(self):
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=2
        with self.assertRaisesRegex(ValueError,'TRUNCATED'):public_get(self.o,'metadata',clock=lambda:T,opener=Fake(b'{}'))
        self.assertEqual(raw_usage(self.o.l,T),2)
    def test_metadata_failure_does_not_block_ws_or_force_reconnect(self):
        # Tiny actual clock window, fake WS and HTTP. Six distinct broad frames
        # must survive one failed metadata call in the independently running task.
        self.o.close();self.o=Observer(self.root/'async');c=self.o.l.get('contract','v1');now=time.time()
        a={k:c[k] for k in ('policy_hash','account_hash','source_hash')};a.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_ASYNC',public_data=True,start_at=now-1,stop_at=now+.5,model_cash_budget=0,live_orders=False)
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def send(self,*a):pass
            async def recv(self):
                await asyncio.sleep(.015);return json.dumps(dict(stream='!miniTicker@arr',data=mini(time.time()-.05)))
        def fail(root,source,symbol):
            if source=='metadata':raise ValueError('RESPONSE_TRUNCATED_AT_BYTE_LIMIT')
            # New clock path receives its own synthetic bounded sample. Metadata
            # still fails; at least two independently eligible prices must pass.
            from one_sentinel.clocks import capture
            o=Observer(root)
            try:
                o.live_approval=o.l.get('trial_approval','v1');sent=capture();received=capture()
                o.ingest('clock',json.dumps(dict(serverTime=int(received['wall']*1000))).encode(),received['wall'],received['wall'],
                    'REAL_FORWARD',context=dict(request_timing=sent),timing=received)
            finally:o.close()
        ws=Mock(return_value=WS())
        with patch('one_sentinel.transport._public_thread',side_effect=fail) as http:
            asyncio.run(smoke(self.o,a,.5,connector=ws))
        self.assertEqual(sum(c.args[1]=='metadata' for c in http.call_args_list),1)
        self.assertEqual(sum(c.args[1]=='clock' for c in http.call_args_list),1);self.assertEqual(ws.call_count,1)
        self.assertGreaterEqual(len(self.o.l.all('input')),2);self.assertEqual(self.o.l.all('stream_gap'),[]);self.assertEqual(len(self.o.l.all('source_gap')),1)
    def test_restart_respects_prior_failed_metadata_cadence(self):
        self.o.close();self.o=Observer(self.root/'restart');c=self.o.l.get('contract','v1');now=time.time()
        a={k:c[k] for k in ('policy_hash','account_hash','source_hash')};a.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_ASYNC',public_data=True,start_at=now-1,stop_at=now+.1,model_cash_budget=0,live_orders=False)
        with self.o.l.transaction():
            self.o.l.add('http_intent','prior',dict(id='prior',at=now-20,source='metadata',read_reservation=0))
            self.o.l.add('http_intent','prior-clock',dict(id='prior-clock',at=now-20,source='clock',read_reservation=0))
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def recv(self):await asyncio.sleep(.12);return '{"result":null,"id":1}'
        with patch('one_sentinel.transport._public_thread') as http:asyncio.run(smoke(self.o,a,.1,connector=Mock(return_value=WS())))
        self.assertEqual(http.call_count,0)

if __name__=='__main__':unittest.main()
