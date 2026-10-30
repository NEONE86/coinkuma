"""Offline original-byte reinterpretation and separate synthetic end-to-end cases."""
import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from one_sentinel.engine import Observer
from one_sentinel.demo import T,SYMBOL,sequence,metadata,mini,bar,book
from one_sentinel.transport import public_get,validate_approval,recover_deliveries

def main():
    p=argparse.ArgumentParser();p.add_argument('--original',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.out.exists():raise ValueError('NEW_OUTPUT_REQUIRED')
    a.out.mkdir(parents=True)
    with patch('one_sentinel.approvals.claim_store',return_value=a.out/'synthetic-claims.sqlite3'):
        replay(a)

def replay(a):
    def no_network(*args,**kw):raise AssertionError('OFFLINE_ONLY')
    socket.socket.connect=no_network;socket.create_connection=no_network
    reports={}
    def finish(o,name,classification):
        dest=a.out/name/'export';o.export(dest);shutil.copytree(o.l.root/'raw',dest/'raw')
        o.close()
        shutil.copyfile(a.out/name/'runtime/observer.sqlite3',dest/'observer.sqlite3')
        verifier=Path(__file__).with_name('verify_sentinel_corrections.py')
        run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(dest)],capture_output=True,text=True,encoding='utf-8')
        (a.out/name/'verification_stdout.txt').write_text(run.stdout,encoding='utf-8')
        (a.out/name/'verification_stderr.txt').write_text(run.stderr,encoding='utf-8')
        if run.returncode:raise RuntimeError('INDEPENDENT_AUDIT_FAILED: '+name+'\n'+run.stderr)
        reports[name]=dict(classification=classification,audit=json.loads(run.stdout))
    def put(o,s,t,value,**kw):return o.ingest(s,json.dumps(value).encode(),t,t,**kw)
    old=[json.loads(s) for s in (a.original/'ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    contract=json.loads(next(r['body'] for r in old if r['kind']=='contract'))
    o=Observer(a.out/'real_reused/runtime')
    for row in old:
        if row['kind']!='receipt':continue
        r=json.loads(row['body']);raw=(a.original/'raw'/(r['sha256']+'.bin')).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==r['sha256']
        provenance=dict(original_receipt_id=r['id'],original_received_at=r['received_at'],original_sha256=r['sha256'],
                        original_sequence=row['seq'],original_source_hash=contract['source_hash'],original_classification=r['classification'],
                        clock_evidence='NO_ORIGINAL_MONOTONIC_OR_CALIBRATION_RECEIPTS')
        o.ingest(r['source'],raw,r['received_at'],time.time(),'REAL_REUSED',r['origin'],provenance=provenance)
    assert len(o.l.all('input'))==1 and len(o.l.all('fill'))==0
    finish(o,'real_reused','REAL_REUSED_NOT_NEW_FORWARD_EVIDENCE')
    o=Observer(a.out/'synthetic_priority/runtime')
    for s,t,d in sequence()[:5]:put(o,s,t,d)
    put(o,'mini',T+6,mini(T+6,'96'));put(o,'mini',T+7,mini(T+7,'94'))
    put(o,'book',T+8,book('94'));assert len(o.l.all('fill'))==1
    o.close();o=Observer(a.out/'synthetic_priority/runtime')
    put(o,'book',T+9,book('94'));put(o,'book',T+9,book('94'))
    assert [f['action'] for f in o.l.all('fill')]==['ENTER','EXIT']
    finish(o,'synthetic_priority','SYNTHETIC')
    o=Observer(a.out/'synthetic_normal/runtime')
    for s,t,d in sequence():put(o,s,t,d)
    finish(o,'synthetic_normal','SYNTHETIC')
    o=Observer(a.out/'synthetic_clock/runtime')
    def st(w,m,session='fixture'):return dict(wall=w,monotonic=m,session_id=session)
    put(o,'metadata',T,metadata('TRADING'))
    put(o,'clock',T+.1,dict(serverTime=int((T+.65)*1000)),timing=st(T+.1,10.1),context=dict(request_timing=st(T,10)))
    r=put(o,'mini',T+.2,mini(T+.5)+[dict(mini(T+.8)[0],s='UNCERTAINUSDT'),dict(mini(T+100)[0],s='FUTUREUSDT')],timing=st(T+.2,10.2))
    assert [i['state'] for i in o.l.get('interpretation',r['id'])['items']]==['VALID','UNCERTAIN','FUTURE']
    b=bar(T+.5);b['s']='BTCUSDT';put(o,'bar',T+.3,b,timing=st(T+.3,10.3))
    finish(o,'synthetic_clock','SYNTHETIC')
    for mode in ('partial','timeout'):
        name='synthetic_http_'+mode;o=Observer(a.out/name/'runtime');c=o.l.get('contract','v1')
        approval={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        approval.update(approved_runtime=str(o.l.root),run_id='synthetic-http-'+mode,approval_reference='SYNTHETIC_ONLY_NO_NETWORK:'+mode,public_data=True,start_at=T-1,stop_at=T+100,model_cash_budget=0,live_orders=False,trial_received_byte_cap=100)
        validate_approval(o,approval,T)
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):
                if mode=='partial':raise IncompleteRead(b'partial-body',20)
                raise TimeoutError('unknown buffered bytes')
        class Fake:
            def open(self,*args,**kw):return Response()
        try:public_get(o,'metadata',clock=lambda:T,opener=Fake())
        except (IncompleteRead,TimeoutError):pass
        else:raise AssertionError('READ_FAILURE_EXPECTED')
        finish(o,name,'SYNTHETIC_TRANSPORT_NO_NETWORK')
    o=Observer(a.out/'synthetic_ack/runtime')
    for s,t,d in sequence()[:3]:put(o,s,t,d)
    out=o.l.all('outbox')[0]
    with o.l.transaction():
        o.l.add('send_intent',out['id'],dict(id=out['id'],at=T+3))
        o.l.add('provider_receipt',out['id'],dict(id=out['id'],provider='SYNTHETIC',ack_at=T+4,phone_received_at=None,actual_delivery=False))
    recover_deliveries(o.l);recover_deliveries(o.l)
    finish(o,'synthetic_ack','SYNTHETIC_ACK_NOT_ACTUAL_DELIVERY')
    (a.out/'results.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({name:dict(status=r['audit']['status'],classification=r['classification'],records=r['audit']['records'],item_states=r['audit']['item_states']) for name,r in reports.items()},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
