"""Bounded sidecar I/O using the existing authority; no trading credentials."""
import hashlib,json,math,time,uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from .autonomy.store import utc,instant,digest,atomic_bytes
from .autonomy.market import ALLOWED,NoRedirect
from .alpha_loop.worker import open_loop
from .alpha_loop.reference.research_loop import ExperimentSpec
from .batch_control import claim_owned,ack_preserving_overrun
from .economic_batches import readonly_rows,implementation_hash

def version(mode='historical'):
    h=hashlib.sha256(implementation_hash().encode())
    for n in ('expansion_io.py','historical_expansion.py')+(('receipt_pilot.py',) if mode=='pilot' else ()):
        h.update((Path(__file__).parent/n).read_bytes())
    return h.hexdigest()

def owned(s,key,spec,seconds,fn,engine='CASHFLOW'):
    """The caller holds the batch lock. Durable results close interrupted acks.

    An interrupted operation without a result is terminal and charged its entire
    reservation. It is never silently restarted with uncertain network spending.
    Registration steps are independently idempotent after partial registration.
    """
    c=s.all('batch_config')[-1];alpha=Path(c['alpha_runtime']);p=readonly_rows(alpha,'alpha_protocol')[0]
    loop=open_loop(alpha,p['config']);started=time.monotonic()
    try:
        if not loop.db.execute('select 1 from experiments where id=?',(key,)).fetchone():
            loop.register_experiment(ExperimentSpec(key,'bounded-economic-extension/'+key,engine,1,spec['registered_at'],spec['deadline'],0,seconds,'HISTORICAL'))
        loop.record_data(key,digest(spec))
        if not loop.db.execute('select 1 from tasks where task_key=?',(key,)).fetchone():
            loop.enqueue_task(key,key,'research',input_hash=digest(spec),cash_units=0,time_seconds=seconds)
        task=loop.get_task(key);old=s.get('extension_operation',key,True)
        if old:
            if task['state']=='CLAIMED':ack_preserving_overrun(loop,task,old,old['elapsed_seconds'],old['success'])
            return old
        interrupted=task['state']=='CLAIMED'
        if task['state']=='QUEUED':task=claim_owned(loop,[key])
        if not task:return dict(status='BUDGET_BLOCKED',id=key)
        if task['state']!='CLAIMED':raise ValueError('TERMINAL_TASK_WITHOUT_RECEIPT')
        try:
            if interrupted:raise ValueError('INTERRUPTED_EXTERNAL_STATE_UNCERTAIN_NO_REPLAY')
            value=fn(started);ok=True
        except Exception as exc:value=dict(status='EXECUTION_ERROR',error=type(exc).__name__+':'+str(exc));ok=False
        elapsed=seconds if interrupted else max(1,math.ceil(time.monotonic()-started))
        old=s.append('extension_operation',key,dict(id=key,spec_hash=digest(spec),completed_at=utc(),success=ok,elapsed_seconds=elapsed,result=value))
        ack_preserving_overrun(loop,task,old,elapsed,ok)
        return old
    finally:loop.close()

class Inputs:
    def __init__(self,s,key,started,seconds,max_attempts=90,max_bytes=24*1024*1024,rolling=False):
        self.s=s;self.key=key;self.started=started;self.seconds=seconds;self.max_attempts=max_attempts;self.max_bytes=max_bytes;self.rolling=rolling
    def attempts(self):
        return [r for r in self.s.all('extension_http_intent') if r['operation_id']==self.key]
    def check(self):
        intents=self.attempts()
        if len(intents)>=self.max_attempts:raise ValueError('HTTP_ATTEMPT_CAP')
        receipts=[r for r in self.s.all('http_receipt') if r.get('operation_id')==self.key]
        if sum(r['bytes'] for r in receipts)>=self.max_bytes:raise ValueError('BYTE_CAP')
        if self.rolling:
            since=instant(utc())-timedelta(days=1)
            rs=[r for r in self.s.all('http_receipt')+self.s.all('retrieval_receipt') if instant(r['received_at'])>=since]
            unknown=[r for r in self.s.all('extension_http_intent') if instant(r['at'])>=since and not self.s.get('http_receipt',r['id'],True)]
            if len(rs)+len(unknown)>=self.max_attempts:raise ValueError('SHARED_ROLLING_24H_HTTP_CAP')
        if time.monotonic()-self.started>self.seconds-12:raise ValueError('OPERATION_TIME_CAP')
    def get(self,host,path,params):
        if path not in ALLOWED.get(urlsplit(host).hostname,set()):raise ValueError('PUBLIC_ENDPOINT_REQUIRED')
        url=host+path+'?'+urlencode(params);cachekey=self.key+':'+digest(url)
        old=self.s.get('extension_input',cachekey,True)
        if old:return old['receipt']
        for attempt in range(3):
            self.check();key=uuid.uuid4().hex;at=utc();begin=time.monotonic()
            self.s.append('extension_http_intent',key,dict(id=key,operation_id=self.key,url=url,at=at,attempt=attempt+1))
            raw=b'';status=None;error=None
            try:
                with build_opener(NoRedirect()).open(Request(url,headers={'User-Agent':'ONE-research/1.0'}),timeout=10) as response:
                    status=response.status;raw=response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise ValueError('RESPONSE_TOO_LARGE')
                data=json.loads(raw)
                if isinstance(data,dict) and data.get('code',0)<0:raise ValueError('EXCHANGE_ERROR')
            except HTTPError as exc:status=exc.code;raw=exc.read(65536);error='HTTP_'+str(status)
            except Exception as exc:error=type(exc).__name__+':'+str(exc)[:180]
            sha=hashlib.sha256(raw).hexdigest();atomic_bytes(self.s.root/'raw'/(sha+'.bin'),raw)
            r=self.s.append('http_receipt',key,dict(id=key,operation_id=self.key,url=url,requested_at=at,received_at=utc(),http_status=status,sha256=sha,bytes=len(raw),error=error,elapsed_seconds=time.monotonic()-begin,attempt=attempt+1,provenance='PUBLIC_HTTP_ACTUAL_RECEIPT'))
            if error is None:
                self.s.append('extension_input',cachekey,dict(id=cachekey,receipt=r));return r
            if status in (400,401,403,404,418,429,451) or attempt==2:raise ValueError(error)
        raise AssertionError('unreachable')
