"""Independent Codex review counterexamples; all records/transports synthetic."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from one_sentinel.demo import T, SYMBOL, sequence, mini, book, bar
from one_sentinel.engine import Observer
from one_sentinel.transport import validate_approval, public_get

ROOT=Path(__file__).resolve().parents[1]

def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))

def approval(observer):
    c=observer.l.get('contract','v1')
    return dict({k:c[k] for k in ('policy_hash','account_hash','source_hash')},
                approval_reference='SYNTHETIC_REVIEW_NO_NETWORK',run_id='synthetic-review-run',
                approved_runtime=str(observer.l.root),public_data=True,start_at=T-1,stop_at=T+1000,
                model_cash_budget=0,live_orders=False,trial_http_attempt_cap=1)

class EmptyResponse:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return b''

class Fake:
    def open(self,*args,**kwargs):return EmptyResponse()

class ReviewCounterexamples(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.o=Observer(self.root/'runtime')
    def tearDown(self):self.o.close();self.tmp.cleanup()
    def put(self,source,at,data):
        return self.o.ingest(source,json.dumps(data).encode(),at,at)
    def prepare_entry(self):
        for s,t,d in sequence()[:4]:self.put(s,t,d)
        self.assertEqual(self.o.status()['paper_fills'],0)
        self.assertTrue(any(d['action']=='ENTER' for d in self.o.l.all('decision')))
    def test_fabricated_input_raw_is_not_an_original_receipt(self):
        self.put('mini',T,mini(T))
        export=self.root/'export';self.o.export(export)
        shutil.copytree(self.o.l.root/'raw',export/'raw')
        command=[sys.executable,'-B','-X','utf8',str(ROOT/'scripts/verify_sentinel_corrections.py'),str(export)]
        good=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(good.returncode,0,good.stderr)
        rows=[json.loads(s) for s in (export/'ledger.jsonl').read_text().splitlines()]
        fabricated=json.dumps(mini(T,'200')).encode();h=hashlib.sha256(fabricated).hexdigest()
        (export/'raw'/(h+'.bin')).write_bytes(fabricated)
        prev='0'*64
        for r in rows:
            body=json.loads(r['body'])
            if r['kind']=='input':
                body['sha256']=h;body['parsed'][SYMBOL]['price']='200'
            r['body']=canonical(body);r['prev']=prev
            r['hash']=hashlib.sha256(canonical([prev,r['kind'],r['id'],body]).encode()).hexdigest();prev=r['hash']
        (export/'ledger.jsonl').write_text(''.join(canonical(r)+'\n' for r in rows))
        meter=json.loads((export/'meter.json').read_text());meter['physical_retained_bytes']+=len(fabricated)
        (export/'meter.json').write_text(json.dumps(meter))
        rejected=subprocess.run(command,capture_output=True,text=True)
        self.assertNotEqual(rejected.returncode,0,'Fabricated raw passed independent audit')
        self.assertIn('INPUT_RECEIPT_BINDING',rejected.stderr)
    def test_one_approval_cannot_reset_http_budget_in_another_runtime(self):
        a=approval(self.o);validate_approval(self.o,a,T)
        public_get(self.o,'metadata',clock=lambda:T,opener=Fake())
        other=Observer(self.root/'other-runtime')
        try:
            with self.assertRaisesRegex(ValueError,'APPROVED_RUNTIME'):
                validate_approval(other,a,T)
                public_get(other,'metadata',clock=lambda:T,opener=Fake())
            self.assertEqual(len(other.l.all('http_intent')),0)
        finally:other.close()
    def test_future_mini_after_enter_prevents_pending_buy(self):
        self.prepare_entry();self.put('mini',T+4,mini(T+100));self.put('book',T+5,book())
        self.assertEqual(self.o.status()['paper_fills'],0)
        self.assertIn('BUY_INPUT_INVALIDATED',[x['reason'] for x in self.o.l.all('execution')])
    def test_valid_pending_buy_still_fills(self):
        self.prepare_entry();self.put('book',T+5,book())
        self.assertEqual(self.o.status()['paper_fills'],1)
    def test_other_symbols_future_data_does_not_block_buy(self):
        self.prepare_entry();data=mini(T+100);data[0]['s']='OTHERUSDT'
        self.put('mini',T+4,data);self.put('book',T+5,book())
        self.assertEqual(self.o.status()['paper_fills'],1)
    def test_future_newer_bar_prevents_pending_buy(self):
        self.prepare_entry();self.put('bar',T+4,bar(T+65));self.put('book',T+5,book())
        self.assertEqual(self.o.status()['paper_fills'],0)
    def test_same_run_resumes_without_resetting_attempts(self):
        a=approval(self.o);validate_approval(self.o,a,T)
        public_get(self.o,'metadata',clock=lambda:T,opener=Fake())
        identity=self.o.l.get('approval_binding','v1')
        self.o.close();self.o=Observer(self.root/'runtime');validate_approval(self.o,a,T+1)
        self.assertEqual(self.o.l.get('approval_binding','v1'),identity)
        with self.assertRaisesRegex(ValueError,'HTTP_CAP'):
            public_get(self.o,'metadata',clock=lambda:T+1,opener=Fake())
    def test_same_path_fresh_ledger_cannot_reconsume_approval(self):
        a=approval(self.o);validate_approval(self.o,a,T)
        self.o.close();old=self.root/'runtime';archive=self.root/'archived-runtime'
        self.assertTrue(old.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(archive.resolve().is_relative_to(self.root.resolve()))
        old.rename(archive);self.o=Observer(old)
        with self.assertRaisesRegex(ValueError,'APPROVAL_ALREADY_CONSUMED'):validate_approval(self.o,a,T+1)
    def test_changing_target_field_does_not_reuse_consumed_reference(self):
        a=approval(self.o);validate_approval(self.o,a,T)
        other=Observer(self.root/'other')
        try:
            with self.assertRaisesRegex(ValueError,'APPROVAL_ALREADY_CONSUMED'):
                validate_approval(other,dict(a,approved_runtime=str(other.l.root)),T)
        finally:other.close()
    def test_unbound_legacy_approval_is_rejected_before_transport(self):
        a=approval(self.o);del a['approved_runtime']
        with self.assertRaisesRegex(ValueError,'APPROVED_RUNTIME'):validate_approval(self.o,a,T)
        self.assertFalse(self.o.l.all('trial_approval'))

if __name__=='__main__':unittest.main()
