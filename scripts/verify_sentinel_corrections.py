"""Independent offline clock/risk/byte audit. No project implementation imports."""
import argparse
import collections
import hashlib
import json
import math
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from verify_pending_epoch import Audit

def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def expected_time(event,receipt,sample,cfg):
    timing=receipt.get('timing')
    if receipt['classification']=='SYNTHETIC' and timing is None:
        return dict(state='VALID' if event<=receipt['received_at'] else 'FUTURE',source_time=event,
                    age_upper=max(0,receipt['received_at']-event),version='SYNTHETIC_EXACT_V1')
    if not timing or not sample:return dict(state='UNVERIFIABLE',reason='NO_CONTEMPORANEOUS_CALIBRATION')
    recv=sample['received'];sent=sample['sent']
    if timing['session_id']!=recv['session_id']:return dict(state='UNVERIFIABLE',reason='NEW_MONOTONIC_EPOCH')
    age=timing['monotonic']-recv['monotonic']
    if age<0:return dict(state='UNVERIFIABLE',reason='CALIBRATION_NOT_YET_AVAILABLE')
    if age>cfg['max_age_seconds']:return dict(state='UNCERTAIN',reason='CALIBRATION_EXPIRED')
    if abs(timing['wall']-recv['wall']-age)>cfg['wall_jump_seconds']+age*cfg['max_drift_ppm']/1e6:return dict(state='UNCERTAIN',reason='CLOCK_WALL_JUMP')
    drift=(age+sample['rtt_seconds'])*cfg['max_drift_ppm']/1e6+sum(s.get('capture_span',0) for s in (timing,recv,sent))
    low=sample['server_ms']/1000+age-drift
    high=(sample['server_ms']+1)/1000+timing['monotonic']-sent['monotonic']+drift
    return dict(state='VALID' if event<=low else 'FUTURE' if event>high else 'UNCERTAIN',
                lower=low,upper=high,source_time=event,age_upper=max(0,high-event),calibration_id=sample['id'],version='SOURCE_INTERVAL_V1')

def verify(root):
    rows=[json.loads(s) for s in (root/'ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    index={};prev='0'*64;counts=collections.Counter();item_counts=collections.Counter();latest={};expected_valid={};positions=collections.defaultdict(Decimal);charges={};known={}
    risk_states={};required_alerts=set();observed_alerts=collections.Counter()
    followup=Audit()
    for n,row in enumerate(rows,1):
        b=json.loads(row['body']);kind=row['kind'];key=row['id']
        assert row['seq']==n and row['prev']==prev and sha(canonical([prev,kind,key,b]).encode())==row['hash'],'CHAIN'
        assert (kind,key) not in index,'ID_REUSE'
        prev=row['hash'];counts[kind]+=1
        if kind=='contract':contract=b;cfg=b['policy']['clock_model']
        followup.check(kind,key,b,index,contract,positions,expected_time)
        if kind=='receipt':
            raw=(root/'raw'/(b['sha256']+'.bin')).read_bytes()
            assert sha(raw)==b['sha256'] and len(raw)==b['bytes'],'ORIGINAL_RAW'
            if b.get('timing'):assert b['timing']['wall']==b['received_at'],'RECEIPT_WALL'
            assert b['received_at']<=b['available_at'],'AVAILABILITY'
            ctx=b.get('context') or {};group='h:'+ctx['http_intent_id'] if ctx.get('http_intent_id') else 'w:'+ctx['ws_intent_id'] if ctx.get('ws_intent_id') else 'r:'+key
            charges[group]=(b['received_at'],b['bytes']);known[group]=b['bytes']
        if kind in ('http_intent','ws_intent'):
            charges[('h:' if kind=='http_intent' else 'w:')+key]=(b['at'],b.get('read_reservation',0))
        if kind=='clock_calibration':
            r=index['receipt',b['evidence_id']];data=json.loads((root/'raw'/(r['sha256']+'.bin')).read_bytes())
            assert r['source']=='clock' and data['serverTime']==b['server_ms'],'CALIBRATION_RAW'
            assert b['received']==r['timing'] and b['sent']==r['context']['request_timing'],'CALIBRATION_TIMING'
            assert b['sent']['session_id']==b['received']['session_id'],'CALIBRATION_SESSION'
            rtt=b['received']['monotonic']-b['sent']['monotonic']
            assert rtt==b['rtt_seconds'] and 0<=rtt<=cfg['max_rtt_seconds'],'CALIBRATION_RTT'
            assert abs(b['received']['wall']-b['sent']['wall']-rtt)<=cfg['wall_jump_seconds'],'CALIBRATION_JUMP'
            assert b['offset_lower']==data['serverTime']/1000-b['received']['wall'],'CALIBRATION_OFFSET'
            assert b['offset_upper']==(data['serverTime']+1)/1000-b['sent']['wall'],'CALIBRATION_OFFSET'
        if kind=='interpretation':
            r=index['receipt',b['receipt_id']];data=json.loads((root/'raw'/(r['sha256']+'.bin')).read_bytes())
            if isinstance(data,dict) and 'stream' in data:data=data['data']
            values=data if r['source']=='mini' else [data]
            assert len(values)==len(b['items']),'ITEM_COUNT'
            duplicate=collections.Counter(x['s'] for x in values if isinstance(x,dict) and isinstance(x.get('s'),str))
            sample=index.get(('clock_calibration',b['calibration_id'])) if b['calibration_id'] else None
            valid={}
            for i,(value,item) in enumerate(zip(values,b['items'])):
                symbol=value.get('s') if isinstance(value,dict) else None
                assert item['index']==i and item['symbol']==symbol,'ITEM_IDENTITY'
                invalid=False
                try:
                    assert isinstance(symbol,str) and symbol.isalnum() and duplicate[symbol]==1
                    assert type(value['E']) is int
                    event=value['E']/1000
                    if r['source']=='mini':
                        assert value['e']=='24hrMiniTicker'
                        for field in ('c','v','q'):
                            num=Decimal(value[field]);assert num.is_finite() and (num>0 if field=='c' else num>=0)
                        assert event>=latest.get(symbol,float('-inf'))
                    else:
                        k=value['k'];assert k['x'] is True and k['i']=='1m' and type(k['T']) is int and k['T']-k['t']==59999
                        assert Decimal(k['q']).is_finite() and Decimal(k['q'])>=0
                        event=max(event,k['T']/1000)
                except (AssertionError,KeyError,TypeError,ValueError,ArithmeticError):invalid=True
                if invalid:assert item['state']=='INVALID','INVALID_ITEM_ACCEPTED'
                else:
                    expected=expected_time(event,r,sample,cfg)
                    if r['source']=='bar':
                        expected['freshness_source_time']=value['k']['T']/1000
                        if 'age_upper' in expected:expected['age_upper']+=event-expected['freshness_source_time']
                    assert all(item.get(k)==v for k,v in expected.items()),'CLOCK_RECOMPUTATION'
                    if expected['state']=='VALID':valid[symbol if r['source']=='mini' else '@bar']=expected
                item_counts[item['state']]+=1
            expected_valid[key]=valid
            names=sorted(valid) if r['source']=='mini' else [data['s']] if valid else []
            assert b['accepted_symbols']==names,'ACCEPTED_SET'
            assert b['available_at']>=r['available_at'] and b['provenance']==r.get('provenance'),'REVISION_PROVENANCE'
        if kind=='input' and b['source'] in ('mini','bar'):
            revision=index.get(('interpretation_release',key)) or index.get(('interpretation',key))
            if b.get('release_id'):
                release=index['interpretation_release',key]
                expected_valid[key]={release['symbol'] if b['source']=='mini' else '@bar':release['time_validity']}
            assert key in expected_valid and expected_valid[key],'NO_ELIGIBLE_TIME'
            assert b['available_at']==revision['available_at'],'FEATURE_AVAILABILITY'
            for name,timing in expected_valid[key].items():
                assert all(b['time_validity'][name].get(k)==v for k,v in timing.items()),'INPUT_CLOCK_BINDING'
            if b['source']=='mini':
                assert set(b['parsed'])==set(expected_valid[key]),'INPUT_SYMBOL_BINDING'
                latest.update({s:v['source_time'] for s,v in b['parsed'].items()})
        if kind=='replacement':
            old=index['decision',key];new=index['decision',b['replacement_decision_id']]
            assert index['execution',key]['reason']=='RISK_PRIORITY_CANCELLED','REPLACEMENT_NOT_CANCELLED'
            ranks={a:i for i,a in enumerate(contract['account']['risk_priority'])}
            assert ranks[new['action']]<=ranks[old['action']],'REPLACEMENT_PRIORITY'
        if kind=='decision' and positions[b['symbol']]>0:
            risky=b['action'] in ('EXIT','REDUCE') or b['reason']=='DATA_GAP_RETAIN_INVENTORY'
            signature=(b['action'],b['reason'],positions[b['symbol']]) if risky else ('CLEAR',)
            if signature!=risk_states.get(b['symbol']) and (risky or b['symbol'] in risk_states):
                risk_states[b['symbol']]=signature
                if risky:required_alerts.add(key)
        if kind=='outbox' and isinstance(b.get('body'),dict) and b['body'].get('decision_id'):
            body=b['body'];d=index['decision',body['decision_id']]
            assert b['kind']=='RISK' and b['origin']=='DETERMINISTIC_RULE','RISK_ORIGIN'
            assert body['risk_reason']==d['reason'] and body['action']==d['action'] and body['evidence_ids']==d['input_ids'],'RISK_DECISION_BINDING'
            assert Decimal(body['holding_qty'])==positions[d['symbol']],'RISK_INVENTORY'
            inputs=[index['input',i] for i in d['input_ids']]
            source='metadata' if d['reason']=='STRUCTURAL_INVALIDATION' else 'mini' if d['reason'] in ('STOP_LOSS','DRAWDOWN_LIMIT','MARKED_EXPOSURE_LIMIT') else None
            cause=next((i for i in inputs if i['source']==source),None)
            assert b['received_at']==(cause['available_at'] if cause else d['at']),'RISK_LATENCY_ORIGIN'
            observed_alerts[d['id']]+=1
        if kind=='fill':positions[b['symbol']]+=Decimal(b['qty'])*(1 if b['side']=='BUY' else -1);assert positions[b['symbol']]>=0,'NEGATIVE_INVENTORY'
        if kind in ('http_result','ws_result'):
            if b.get('raw_hash') and b.get('bytes'):
                raw=(root/'raw'/(b['raw_hash']+'.bin')).read_bytes();assert sha(raw)==b['raw_hash'] and len(raw)==b['bytes'],'TRANSPORT_RAW'
            assert b.get('upper_bound_charge',b['bytes'])>=b['bytes'],'UNDERCHARGED_BYTES'
            if b.get('error_type')=='TimeoutError':assert b.get('accounting_state')=='UNCERTAIN','TIMEOUT_UNCERTAINTY_RELEASED'
            intent=index['http_intent' if kind=='http_result' else 'ws_intent',key]
            expected=intent['read_reservation'] if b.get('accounting_state')=='UNCERTAIN' else b['bytes']
            assert b.get('upper_bound_charge',b['bytes'])==expected,'UNCERTAINTY_RESERVATION'
            group=('h:' if kind=='http_result' else 'w:')+key
            charges[group]=(b['received_at'],expected);known[group]=b['bytes']
        index[kind,key]=b
    assert required_alerts==set(observed_alerts) and all(n==1 for n in observed_alerts.values()),'MISSING_OR_DUPLICATE_RISK_ALERT'
    followup.finish(index,contract)
    for (kind,key),body in index.items():
        if kind=='supersession':assert ('replacement',key) in index,'UNLINKED_REPLACEMENT'
    meter=json.loads((root/'meter.json').read_text()) if (root/'meter.json').exists() else None
    if meter:
        total=sum(amount for at,amount in charges.values() if at>=meter['as_of']-86400)
        assert total==meter['rolling_24h_upper_bound_charge'],'BYTE_CHARGE_RECOMPUTATION'
        assert sum(known.values())==meter['known_received_application_bytes'],'KNOWN_BYTES_RECOMPUTATION'
        assert sum(p.stat().st_size for p in (root/'raw').glob('*.bin'))==meter['physical_retained_bytes'],'PHYSICAL_BYTES'
    base=json.loads(subprocess.check_output([sys.executable,'-B','-X','utf8',str(Path(__file__).with_name('verify_sentinel.py')),str(root)],text=True,encoding='utf-8'))
    return dict(status='PASS_INDEPENDENT_CLOCK_RISK_RAW_RECOMPUTATION',records=len(rows),record_counts=dict(counts),item_states=dict(item_counts),account=base,
                byte_meter=meter,network_calls=0,investment_edge_validated=False)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args()
    print(json.dumps(verify(a.bundle.resolve()),ensure_ascii=False,indent=2))
