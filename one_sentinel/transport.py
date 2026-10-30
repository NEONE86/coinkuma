"""Approval-bound public-data smoke adapter and one HTTPS webhook channel.

No trading API, scheduler installation or paid model. Never enabled by default.
Tests inject transports; a local approval file is an audit binding, not evidence
that a person approved it. The operator must obtain approval before creating it.
"""
import asyncio,hashlib,json,time,uuid
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.parse import urlsplit,urlencode
from urllib.error import HTTPError,URLError
from http.client import IncompleteRead
import socket
from .store import Ledger,digest,raw_usage,halt_observation
from .clocks import capture, SESSION_ID
from .paper import account
from .notify import analyze_one
from .approvals import bind as bind_approval, verify_binding

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('REDIRECT_FORBIDDEN')

def validate_approval(observer,value,now,timing=None):
    contract=observer.l.get('contract','v1')
    if not value.get('approval_reference') or value.get('policy_hash')!=contract['policy_hash'] or value.get('account_hash')!=contract['account_hash'] or value.get('source_hash')!=contract['source_hash']:raise ValueError('EXACT_APPROVAL_REQUIRED')
    if not value.get('public_data') or not value['start_at']<=now<value['stop_at'] or value['stop_at']-value['start_at']>172800:raise ValueError('TRIAL_WINDOW')
    if value.get('model_cash_budget')!=0 or value.get('live_orders') is not False:raise ValueError('NO_SPENDING_OR_ORDERS')
    bind_approval(observer.l,value)
    with observer.l.transaction():
        observer.l.add('trial_approval','v1',value)
        if timing:
            observer.l.add('trial_session',timing['session_id'],dict(id=timing['session_id'],
                started=timing,monotonic_deadline=timing['monotonic']+value['stop_at']-now))
    observer.live_approval=value
    return value

def trial_active(observer,stamp):
    a=observer.live_approval
    if not a or not a['start_at']<=stamp['wall']<a['stop_at']:return False
    session=observer.l.get('trial_session',stamp['session_id'])
    if session:
        start=session['started'];elapsed=stamp['monotonic']-start['monotonic']
        if elapsed<0 or stamp['monotonic']>=session['monotonic_deadline']:return False
        if abs((stamp['wall']-start['wall'])-elapsed)>observer.policy['clock_model']['wall_jump_seconds']+elapsed*observer.policy['clock_model']['max_drift_ppm']/1e6:
            halt_observation(observer.l,stamp['wall'],'CLOCK_WALL_JUMP');return False
    return True

def received_cap(observer):
    return min(observer.policy['proposed_new_trial_limits']['raw_bytes_per_day'],
               (observer.live_approval or {}).get('trial_received_byte_cap',float('inf')))

def public_get(observer,source,symbol=None,clock=time.time,opener=None):
    sent=capture(clock);now=sent['wall'];a=observer.live_approval
    if not trial_active(observer,sent):raise ValueError('TRIAL_WINDOW')
    verify_binding(observer.l,a)
    if source not in ('metadata','book','clock'):raise ValueError('PUBLIC_PATH_ONLY')
    if symbol is not None and (not symbol.isalnum() or not symbol.endswith('USDT')):raise ValueError('SYMBOL')
    if raw_usage(observer.l,now)>=received_cap(observer):
        halt_observation(observer.l,now,'RAW_DAILY_LIMIT');raise ValueError('RAW_DAILY_LIMIT')
    with observer.l.transaction():
        attempts=[r for r in observer.l.all('http_intent') if r['at']>=now-86400]
        # 288 scheduled metadata + 400 books; 312 recovery room is NOT auto spent.
        if len(attempts)>=688:raise ValueError('HTTP_CAP')
        if len(attempts)>=(a.get('trial_http_attempt_cap',688)):raise ValueError('HTTP_CAP')
        if source=='book' and sum(r['source']=='book' for r in attempts)>=400:raise ValueError('BOOK_HTTP_CAP')
        if source=='book' and sum(r['source']=='book' for r in attempts)>=a.get('trial_book_attempt_cap',400):raise ValueError('BOOK_HTTP_CAP')
        room=received_cap(observer)-raw_usage(observer.l,now)
        if room<=0:raise ValueError('RAW_DAILY_LIMIT')
        limit=min(1024 if source=='clock' else 65536 if source=='book' else 16*1024*1024+1,room)
        key=uuid.uuid4().hex;observer.l.add('http_intent',key,dict(id=key,at=now,source=source,symbol=symbol,read_reservation=limit,
            request_timing=sent,monotonic_deadline=sent['monotonic']+a['stop_at']-now))
    url=observer.policy['sources'][source]['url']
    if source=='book':url+='?'+urlencode(dict(symbol=symbol,limit=20))
    start=clock();raw=b'';http_status=None;receipt=None;truncated=False
    uncertain=False;stage='OPEN';response=None
    def read_body(response):
        nonlocal raw,uncertain,stage
        stage='READ'
        # urllib does not transparently decompress. Demand identity encoding so
        # retained application bytes and decoded body bytes cannot be conflated.
        headers=getattr(response,'headers',{})
        if headers.get('Content-Encoding','identity').lower() not in ('identity',''):
            uncertain=True;raise ValueError('UNSUPPORTED_CONTENT_ENCODING')
        while len(raw)<limit:
            if not trial_active(observer,capture(clock)):
                uncertain=True;raise ValueError('TRIAL_WINDOW')
            size=min(65536,limit-len(raw))
            try:chunk=response.read(size)
            except IncompleteRead as exc:
                partial=exc.partial or b''
                if len(partial)>size:
                    raw+=partial[:size];uncertain=True
                else:raw+=partial
                raise
            except Exception:
                uncertain=True;raise
            if not isinstance(chunk,bytes) or len(chunk)>size:
                uncertain=True;raise ValueError('INVALID_BOUNDED_READ')
            raw+=chunk
            if len(chunk)<size:break # HTTPResponse.read(n) returns short only at EOF.
    try:
        try:
            response=(opener or build_opener(NoRedirect())).open(Request(url,headers={'User-Agent':'ONE-observer-paper/1','Accept-Encoding':'identity'}),timeout=min(10,max(.001,a['stop_at']-clock())))
        except HTTPError as exc:
            response=exc
        with response:
            http_status=getattr(response,'status',getattr(response,'code',None));read_body(response)
        truncated=len(raw)>=limit
        if truncated:raise ValueError('RESPONSE_TRUNCATED_AT_BYTE_LIMIT')
        if http_status!=200:raise ValueError('HTTP_STATUS')
        received_stamp=capture(clock);received=received_stamp['wall']
        if not trial_active(observer,received_stamp):raise ValueError('TRIAL_WINDOW')
        receipt=observer.ingest(source,raw,received,received,'REAL_FORWARD',url,
            context=dict(symbol=symbol,http_intent_id=key,request_timing=sent),timing=received_stamp,processing_clock=clock)
        with observer.l.transaction():observer.l.add('http_result',key,dict(id=key,received_at=received,elapsed=received-start,raw_hash=hashlib.sha256(raw).hexdigest(),bytes=len(raw),known_received_bytes=len(raw),upper_bound_charge=len(raw),uncertain_reserved_bytes=0,accounting_state='KNOWN',input_receipt_id=receipt['id'],http_status=http_status,truncated=False,status='QUARANTINED' if observer.l.get('quarantine',receipt['id']) else 'RECEIVED'))
    except Exception as exc:
        # Preserve malformed/error body bytes when available, without provider secrets.
        h=hashlib.sha256(raw).hexdigest();(observer.l.root/'raw'/f'{h}.bin').write_bytes(raw)
        code=str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else None
        # Refused connection/DNS failure establishes zero body bytes. An open
        # timeout need not: headers/internal buffers may already have arrived.
        cause=exc.reason if isinstance(exc,URLError) else exc
        if stage=='OPEN' and not isinstance(cause,(ConnectionRefusedError,socket.gaierror)):
            uncertain=True
        charge=limit if uncertain else len(raw)
        with observer.l.transaction():observer.l.add('http_result',key,dict(id=key,received_at=clock(),status='ERROR',error_type=type(exc).__name__,error_code=code,raw_hash=h,bytes=len(raw),known_received_bytes=len(raw),upper_bound_charge=charge,uncertain_reserved_bytes=charge-len(raw),accounting_state='UNCERTAIN' if uncertain else 'KNOWN',input_receipt_id=receipt['id'] if receipt else None,http_status=http_status,truncated=truncated))
        raise

def recover_deliveries(ledger):
    """Local projection only: no approval, provider call, or inferred phone ACK."""
    with ledger.transaction():
        for ack in ledger.all('provider_receipt'):
            if not ledger.get('delivery',ack['id']):ledger.add('delivery',ack['id'],ack)

def send_webhook(root,url,clock=time.time,opener=None):
    """At-most-one uncertain send; provider dedup/phone receipt NOT assumed."""
    l=Ledger(root)
    try:
        recover_deliveries(l)
        pending=[r for r in l.all('outbox') if not l.get('send_intent',r['id']) and not l.get('delivery',r['id'])]
        if not pending:return None
        a=l.get('trial_approval','v1');now=clock()
        if not a or not a['start_at']<=now<a['stop_at'] or a.get('channel_url_sha256')!=hashlib.sha256(url.encode()).hexdigest():raise ValueError('CHANNEL_APPROVAL_REQUIRED')
        verify_binding(l,a)
        parts=urlsplit(url)
        if parts.scheme!='https' or parts.username or parts.password:raise ValueError('HTTPS_CHANNEL_REQUIRED')
        with l.transaction():
            pending=[r for r in l.all('outbox') if not l.get('send_intent',r['id']) and not l.get('delivery',r['id'])]
            if not pending:return None
            r=pending[0];key=r['id'];l.add('send_intent',key,dict(id=key,at=now,channel_hash=a['channel_url_sha256']))
        try:
            req=Request(url,data=json.dumps(dict(event_id=r['event_id'],notification_id=key,kind=r['kind'],revision=r['revision'],body=r['body'])).encode(),headers={'Content-Type':'application/json','Idempotency-Key':key},method='POST')
            with (opener or build_opener(NoRedirect())).open(req,timeout=5) as response:
                if not 200<=response.status<300:raise ValueError('PROVIDER_STATUS')
                raw=response.read(65536)
            at=clock();ack=dict(id=key,provider='APPROVED_HTTPS_WEBHOOK',ack_at=at,latency_seconds=at-r['received_at'],response_hash=hashlib.sha256(raw).hexdigest(),phone_received_at=None,actual_delivery=True)
            with l.transaction():l.add('provider_receipt',key,ack)
            with l.transaction():l.add('delivery',key,ack)
            return ack
        except Exception as exc:
            with l.transaction():l.add('delivery_unknown',key,dict(id=key,at=clock(),error_type=type(exc).__name__,retry_requires_reconciliation=True))
            return None
    finally:l.close()

async def smoke(observer,approval,seconds,channel_url=None,connector=None):
    """Bounded, manually invoked trial. Basic delivery runs independently of I/O.

    One combined WS connection: broad miniTicker + <=5 closed 1m bar streams.
    Unknown sends are retained for reconciliation; no claim of exactly-once delivery.
    """
    if connector is None:
        from websockets.asyncio.client import connect
    else:connect=connector # injected only by offline regression tests
    started=capture();validate_approval(observer,approval,started['wall'],started)
    end=started['monotonic']+min(seconds,approval['stop_at']-started['wall'])
    last_rotation=0;streams=[];stopped=asyncio.Event()
    def active():
        stamp=capture()
        return not stopped.is_set() and stamp['monotonic']<end and trial_active(observer,stamp)
    def budget_stop(exc):
        if str(exc) in ('RAW_DAILY_LIMIT','HTTP_CAP','BOOK_HTTP_CAP','TRIAL_WINDOW'):
            if str(exc)!='TRIAL_WINDOW':halt_observation(observer.l,time.time(),str(exc))
            stopped.set();return True
        return False
    async def notify_loop():
        while active():
            if channel_url:await asyncio.to_thread(send_webhook,observer.l.root,channel_url)
            await asyncio.sleep(1)
    # Independent task: failed or slow HTTP cannot block WS observation or force
    # reconnects. Cadence follows attempts, including failures and restarts.
    async def source_loop(source):
        cadence=observer.policy['sources'][source]['cadence_seconds']
        while active():
            attempts=[r['at'] for r in observer.l.all('http_intent') if r['source']==source]
            due=max(attempts)+cadence if attempts else 0
            if time.time()<due:
                await asyncio.sleep(min(.1,max(0,end-time.monotonic())));continue
            try:await asyncio.to_thread(_public_thread,observer.l.root,source,None)
            except Exception as exc:
                with observer.l.transaction():observer.l.add('source_gap',uuid.uuid4().hex,dict(at=time.time(),source=source,error_type=type(exc).__name__))
                if budget_stop(exc):return
                # Pre-request budget blocks produce no intent; avoid a hot loop.
                until=min(time.monotonic()+cadence,end)
                while active() and time.monotonic()<until:await asyncio.sleep(.05)
            else:
                until=min(time.monotonic()+cadence,end)
                while active() and time.monotonic()<until:await asyncio.sleep(.05)
    notifier=asyncio.create_task(notify_loop())
    source_tasks=[asyncio.create_task(source_loop(s)) for s in ('metadata','clock')]
    ws_pending=None
    def ws_reserve():
        # One reserved application message, shared with HTTP; no read/reconnect
        # after capacity is unavailable. TLS/network overhead is not measured.
        key=uuid.uuid4().hex;at=time.time();limit=16*1024*1024
        with observer.l.transaction():
            if raw_usage(observer.l,at)+limit>received_cap(observer):raise ValueError('RAW_DAILY_LIMIT')
            observer.l.add('ws_intent',key,dict(id=key,at=at,read_reservation=limit))
        return key
    def ws_settle(key,raw,received,receipt=None,disposition='IGNORED',uncertain=False):
        if raw:(observer.l.root/'raw'/f'{hashlib.sha256(raw).hexdigest()}.bin').write_bytes(raw)
        with observer.l.transaction():observer.l.add('ws_result',key,dict(id=key,received_at=received,
            bytes=len(raw),known_received_bytes=len(raw),upper_bound_charge=16*1024*1024 if uncertain else len(raw),
            accounting_state='UNCERTAIN' if uncertain else 'KNOWN',raw_hash=hashlib.sha256(raw).hexdigest(),
            input_receipt_id=receipt['id'] if receipt else None,disposition=disposition))
    try:
        while active():
            try:
                ws_pending=ws_reserve()
                async with connect('wss://stream.binance.com:9443/stream?streams=!miniTicker@arr',open_timeout=10,close_timeout=3,max_size=16*1024*1024,max_queue=1,ping_interval=20) as ws:
                    streams=[]
                    while active():
                        now=time.time()
                        rows=observer.l.all('membership');wanted=[s.lower()+'@kline_1m' for s in (rows[-1]['symbols'] if rows else [])]
                        if wanted!=streams and now-last_rotation>=5:
                            if streams:await ws.send(json.dumps(dict(method='UNSUBSCRIBE',params=streams,id=1)))
                            if wanted:await ws.send(json.dumps(dict(method='SUBSCRIBE',params=wanted,id=2)))
                            streams=wanted;last_rotation=now
                        if ws_pending is None:ws_pending=ws_reserve()
                        try:message=await asyncio.wait_for(ws.recv(),min(1,max(.001,end-time.monotonic())))
                        except asyncio.TimeoutError:
                            if active():
                                stamp=capture();observer.tick(stamp['wall'],stamp)
                            continue
                        stamp=capture();received=stamp['wall'];raw=message.encode() if isinstance(message,str) else message
                        receipt=None;disposition='AFTER_WINDOW'
                        try:
                            if not active():break
                            payload=json.loads(raw);data=payload.get('data')
                            disposition='ACK_OR_UNCLOSED'
                            if data is None:continue
                            source='mini' if isinstance(data,list) else 'bar'
                            if source=='bar' and not data.get('k',{}).get('x'):continue
                            receipt=observer.ingest(source,raw,received,received,'REAL_FORWARD',
                                'wss://stream.binance.com:9443/stream',context=dict(ws_intent_id=ws_pending),timing=stamp)
                            disposition='QUARANTINED' if observer.l.get('quarantine',receipt['id']) else 'RECEIVED'
                        finally:
                            ws_settle(ws_pending,raw,received,receipt,disposition);ws_pending=None
                        # Normalization may finish after receipt. Never rewind
                        # the decision clock to the pre-normalization WS stamp.
                        processed=capture()
                        observer.tick(processed['wall'],processed)
                        analyze_one(observer.l,observer.policy,processed['wall'])
                        for d in account(observer.l,observer.config)['pending']:
                            if d['ready_at']<=time.time()<=d['expires_at']:
                                try:await asyncio.to_thread(_public_thread,observer.l.root,'book',d['symbol'])
                                except Exception as exc:
                                    with observer.l.transaction():observer.l.add('source_gap',uuid.uuid4().hex,dict(at=time.time(),source='book',error_type=type(exc).__name__))
                                    budget_stop(exc)
            except Exception as exc:
                if ws_pending:
                    ws_settle(ws_pending,b'',time.time(),disposition='CONNECTION_OR_READ_UNKNOWN',uncertain=True);ws_pending=None
                if budget_stop(exc):break
                with observer.l.transaction():observer.l.add('stream_gap',uuid.uuid4().hex,dict(at=time.time(),error_type=type(exc).__name__))
                stamp=capture();observer.tick(stamp['wall'],stamp);await asyncio.sleep(min(5,max(0,end-time.monotonic())))
    finally:
        stopped.set()
        if ws_pending:ws_settle(ws_pending,b'',time.time(),disposition='WINDOW_CLOSED_UNDELIVERED_BUFFER',uncertain=True)
        notifier.cancel()
        try:await notifier
        except asyncio.CancelledError:pass
        # An in-flight bounded HTTP thread finishes and records its receipt.
        # Cancelling to_thread would leave it writing after the observer closes.
        await asyncio.gather(*source_tasks)

def _public_thread(root,source,symbol):
    from .engine import Observer
    o=Observer(root)
    try:o.live_approval=o.l.get('trial_approval','v1');public_get(o,source,symbol)
    finally:o.close()
