"""Bounded, same-epoch release of initially well-formed boundary uncertainty.

Original receipt/interpretation never changes. A new availability fact is not a
claim that the value was eligible at arrival. FUTURE/UNVERIFIABLE never enter here.
"""
import hashlib
import json
from decimal import Decimal
from .clocks import assess,interval,capture,valid_stamp
from .observe import parse,membership
from .paper import account
from .store import digest

VERSION='BOUNDED_PENDING_RELEASE_V1'

def active(ledger):
    return [json.loads(r[0]) for r in ledger.db.execute('SELECT r.body FROM clock_pending_index p JOIN records r ON r.seq=p.seq ORDER BY p.seq')]

def meter(ledger):
    rows=active(ledger)
    return dict(active_items=len(rows),active_envelope_bytes=sum({r['receipt_id']:r['envelope_bytes'] for r in rows}.values()),
                outcomes={state:n for state,n in ledger.db.execute("SELECT json_extract(body,'$.reason'),count(*) FROM records WHERE kind='clock_pending_result' GROUP BY json_extract(body,'$.reason')")})

def finish(o,p,at,reason,timing=None,release_id=None):
    same=valid_stamp(timing) and timing['session_id']==p['timing']['session_id']
    o.l.add('clock_pending_result',p['id'],dict(id=p['id'],receipt_id=p['receipt_id'],symbol=p['symbol'],
        at=at,state='RELEASED' if release_id else 'DROPPED',reason=reason,release_id=release_id,
        timing=timing,wait_seconds=timing['monotonic']-p['timing']['monotonic'] if same else None))

def enqueue(o,r,items,sample,at):
    if r['classification']=='REAL_REUSED':return
    cfg=o.policy['clock_pending']
    for item in items:
        if item['state']!='UNCERTAIN' or 'lower' not in item or not sample or not valid_stamp(r.get('timing')):continue
        key=digest([VERSION,r['id'],item['index']])
        if o.l.get('clock_pending',key):continue
        rows=active(o.l);charged={v['receipt_id']:v['envelope_bytes'] for v in rows};charged[r['id']]=r['bytes']
        p=dict(id=key,version=VERSION,receipt_id=r['id'],initial_interpretation_id=r['id'],
            item_index=item['index'],symbol=item['symbol'],source=r['source'],source_time=item['source_time'],
            timing=r['timing'],created_at=at,calibration_id=sample['id'],envelope_bytes=r['bytes'],
            ttl_seconds=cfg['ttl_seconds'])
        o.l.add('clock_pending',key,p)
        if len(rows)>=cfg['max_items']:finish(o,p,at,'CAPACITY_ITEMS',r['timing'])
        elif sum(charged.values())>cfg['max_envelope_bytes']:finish(o,p,at,'CAPACITY_BYTES',r['timing'])

def resolve(o,now,stamp,trigger):
    if not valid_stamp(stamp):return
    cache={}
    for p in active(o.l):
        r=o.l.get('receipt',p['receipt_id'])
        if stamp['session_id']!=p['timing']['session_id']:
            finish(o,p,now,'NEW_MONOTONIC_EPOCH',stamp);continue
        if stamp['wall']!=now or now<p['created_at']:
            finish(o,p,now,'CLOCK_WALL_JUMP',stamp);continue
        initial=o.l.get('clock_calibration',p['calibration_id'])
        bounds=interval(stamp,initial,o.policy['clock_model'])
        if bounds['state']!='BOUNDED':
            finish(o,p,now,bounds['reason'],stamp);continue
        elapsed=stamp['monotonic']-p['timing']['monotonic']
        if elapsed<0:finish(o,p,now,'MONOTONIC_REGRESSION',stamp);continue
        if elapsed>=p['ttl_seconds']:finish(o,p,now,'PENDING_TTL',stamp);continue
        sample=o.l.last('clock_calibration')
        t=assess(p['source_time'],stamp,sample,o.policy['clock_model'])
        if t['state']=='UNCERTAIN' and 'lower' in t:continue
        if t['state']!='VALID':finish(o,p,now,t.get('reason','RECALIBRATED_'+t['state']),stamp);continue
        if r['id'] not in cache:
            raw=(o.l.root/'raw'/(r['sha256']+'.bin')).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256'] or len(raw)!=r['bytes']:
                finish(o,p,now,'RAW_CORRUPTION',stamp);continue
            data=json.loads(raw)
            cache[r['id']]=data['data'] if isinstance(data,dict) and 'stream' in data else data
        envelope=cache[r['id']];value=envelope[p['item_index']] if r['source']=='mini' else envelope
        data=parse(r['source'],[value] if r['source']=='mini' else value,float('inf'))
        old=o.l.latest_input(r['source'],p['symbol'])
        if r['source']=='mini' and old and data[p['symbol']]['source_time']<old['parsed'][p['symbol']]['source_time']:
            finish(o,p,now,'OUT_OF_ORDER_MARKET_TIME',stamp);continue
        if r['source']=='bar':
            t['freshness_source_time']=value['k']['T']/1000;t['age_upper']+=p['source_time']-t['freshness_source_time']
            deep=membership(o.l,o.policy,now,[s for s,v in account(o.l,o.config)['positions'].items() if v['qty']])
            if p['symbol'] not in deep:finish(o,p,now,'NOT_IN_DEEP_SUBSCRIPTION',stamp);continue
            rejection=o.l.bar_rejection(data)
            if rejection:finish(o,p,now,rejection,stamp);continue
        done=capture() if r['classification']=='REAL_FORWARD' else stamp
        age=t['age_upper']+done['monotonic']-stamp['monotonic']
        ttl=o.policy['price_fresh_seconds' if r['source']=='mini' else 'bar_fresh_seconds']
        if done['session_id']!=stamp['session_id'] or abs((done['wall']-stamp['wall'])-(done['monotonic']-stamp['monotonic']))>o.policy['clock_model']['wall_jump_seconds']:
            finish(o,p,done['wall'],'CLOCK_CHANGED_DURING_RELEASE',done);continue
        if done['monotonic']-p['timing']['monotonic']>=p['ttl_seconds']:finish(o,p,done['wall'],'PENDING_TTL',done);continue
        if age>ttl:finish(o,p,done['wall'],'STALE_AT_RELEASE',done);continue
        key=digest([VERSION,p['id']]);at=done['wall']
        revision=dict(id=key,version=VERSION,pending_id=p['id'],receipt_id=r['id'],initial_interpretation_id=r['id'],
            item_index=p['item_index'],symbol=p['symbol'],source=r['source'],calibration_id=sample['id'],
            proof_timing=stamp,processing_timing=done,available_at=at,trigger=trigger,time_validity=t,
            received_at=r['received_at'],wait_seconds=at-r['received_at'])
        o.l.add('interpretation_release',key,revision)
        finish(o,p,at,'SAFE_PAST_AND_FRESH',done,key)
        times={p['symbol'] if r['source']=='mini' else '@bar':t}
        row=o.l.add('input',key,dict(r,id=key,receipt_id=r['id'],release_id=key,available_at=at,
            validity_timing=stamp,processing_timing=done,parsed=data,time_validity=times,parser_version=VERSION))
        o.current_timing=done
        if r['source']=='mini' and old and abs(Decimal(data[p['symbol']]['price'])/Decimal(old['parsed'][p['symbol']]['price'])-1)>=Decimal('.02'):
            o.l.add('price_move',key,dict(id=key,symbol=p['symbol'],at=at,receipt_id=key,previous_receipt=old['id']))
        o._after_input(row,old,at)
