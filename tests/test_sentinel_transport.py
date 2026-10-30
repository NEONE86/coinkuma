"""All network/provider objects below are fake; no actual transmissions."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from one_sentinel.engine import Observer
from one_sentinel.transport import validate_approval,public_get,send_webhook
from one_sentinel.demo import T,metadata,sequence

class Response:
    status=200
    def __init__(self,raw):self.raw=raw
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):return self.raw[:n]
class Fake:
    def __init__(self,raw=b'{}',error=False):self.raw=raw;self.error=error;self.calls=0
    def open(self,*a,**kw):
        self.calls+=1
        if self.error:raise TimeoutError('synthetic timeout')
        return Response(self.raw)

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.o=Observer(Path(self.tmp.name)/'r');self.url='https://example.invalid/test-channel'
        c=self.o.l.get('contract','v1');self.approval={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        self.approval.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_TEST_NOT_REAL_APPROVAL',public_data=True,start_at=T-1,stop_at=T+1000,model_cash_budget=0,live_orders=False,channel_url_sha256=hashlib.sha256(self.url.encode()).hexdigest())
    def tearDown(self):self.o.close();self.tmp.cleanup()
    def approve(self):validate_approval(self.o,self.approval,T)
    def event(self):
        for s,t,d in sequence()[:3]:self.o.ingest(s,json.dumps(d).encode(),t,t)
    def test_missing_approval_never_requests(self):
        f=Fake()
        with self.assertRaises(ValueError):public_get(self.o,'metadata',clock=lambda:T,opener=f)
        self.assertEqual(f.calls,0)
    def test_exact_source_and_contract_required(self):
        with self.assertRaisesRegex(ValueError,'EXACT_APPROVAL'):validate_approval(self.o,dict(self.approval,source_hash='wrong'),T)
    def test_no_orders_or_model_spending(self):
        for fields in [dict(live_orders=True),dict(model_cash_budget=1)]:
            with self.assertRaisesRegex(ValueError,'NO_SPENDING'):validate_approval(self.o,dict(self.approval,**fields),T)
    def test_stop_forbids_new_http(self):
        self.approve();f=Fake()
        with self.assertRaisesRegex(ValueError,'TRIAL_WINDOW'):public_get(self.o,'metadata',clock=lambda:T+1001,opener=f)
        self.assertEqual(f.calls,0)
    def test_received_metadata_raw_preserved(self):
        self.approve();raw=json.dumps(metadata()).encode();f=Fake(raw);public_get(self.o,'metadata',clock=lambda:T,opener=f)
        self.assertEqual(f.calls,1);self.assertEqual(self.o.l.all('http_result')[0]['status'],'RECEIVED');self.assertEqual(self.o.l.all('input')[0]['sha256'],hashlib.sha256(raw).hexdigest())
    def test_http_failure_consumes_attempt(self):
        self.approve();f=Fake(error=True)
        with self.assertRaises(TimeoutError):public_get(self.o,'metadata',clock=lambda:T,opener=f)
        self.assertEqual(len(self.o.l.all('http_intent')),1);self.assertEqual(self.o.l.all('http_result')[0]['status'],'ERROR')
    def test_book_attempt_cap_preserves_recovery_room(self):
        self.approve()
        with self.o.l.transaction():
            for i in range(400):self.o.l.add('http_intent',str(i),dict(id=str(i),at=T,source='book'))
        f=Fake()
        with self.assertRaisesRegex(ValueError,'BOOK_HTTP_CAP'):public_get(self.o,'book','BTCUSDT',clock=lambda:T,opener=f)
        self.assertEqual(f.calls,0)
    def test_channel_exact_url_required(self):
        self.approve();self.event();f=Fake()
        with self.assertRaisesRegex(ValueError,'CHANNEL_APPROVAL'):send_webhook(self.o.l.root,'https://example.invalid/wrong',clock=lambda:T+4,opener=f)
        self.assertEqual(f.calls,0)
    def test_ack_and_idempotence_phone_not_claimed(self):
        self.approve();self.event();f=Fake(b'accepted');a=send_webhook(self.o.l.root,self.url,clock=lambda:T+4,opener=f)
        send_webhook(self.o.l.root,self.url,clock=lambda:T+5,opener=f);self.assertEqual(f.calls,1);self.assertIsNone(a['phone_received_at'])
    def test_unknown_send_not_retried(self):
        self.approve();self.event();f=Fake(error=True);send_webhook(self.o.l.root,self.url,clock=lambda:T+4,opener=f)
        send_webhook(self.o.l.root,self.url,clock=lambda:T+5,opener=f);self.assertEqual(f.calls,1);self.assertEqual(len(self.o.l.all('delivery_unknown')),1)
    def test_durable_ack_recovered_without_network(self):
        self.approve();self.event();r=self.o.l.all('outbox')[0]
        with self.o.l.transaction():
            self.o.l.add('send_intent',r['id'],dict(id=r['id'],at=T+3))
            self.o.l.add('provider_receipt',r['id'],dict(id=r['id'],ack_at=T+4,phone_received_at=None))
        f=Fake();send_webhook(self.o.l.root,self.url,clock=lambda:T+5,opener=f);self.assertEqual(f.calls,0);self.assertEqual(len(self.o.l.all('delivery')),1)
    def test_mixed_synthetic_and_real_cohort_forbidden(self):
        self.approve();self.event();f=Fake(json.dumps(metadata()).encode())
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):public_get(self.o,'metadata',clock=lambda:T+4,opener=f)
    def test_combined_ws_keeps_original_envelope(self):
        from one_sentinel.demo import mini
        data=dict(stream='!miniTicker@arr',data=mini(T));raw=json.dumps(data).encode();r=self.o.ingest('mini',raw,T,T)
        self.assertEqual((self.o.l.root/'raw'/f"{r['sha256']}.bin").read_bytes(),raw)

if __name__=='__main__':unittest.main()
