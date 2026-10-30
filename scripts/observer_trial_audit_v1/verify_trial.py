"""Independent, read-only public trial audit. No Observer imports or network I/O."""
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys

SOURCE='00601caa13df0b19e7853f457715cadcfbef0882756dec26bdfba9bc17593338'
COMMIT='2d67741ae6624881d02619f1513a2d556bc114fe'
def read(p): return json.loads(p.read_text(encoding='utf-8'))
def sha(raw): return hashlib.sha256(raw).hexdigest()
def utc(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def stats(values):
    values=sorted(values)
    if not values:return dict(n=0,min=None,p50=None,p95=None,max=None)
    return dict(n=len(values),min=values[0],p50=values[math.ceil(.5*len(values))-1],
                p95=values[math.ceil(.95*len(values))-1],max=values[-1])

def verify(root):
    sys.path.insert(0,str(root/'source/scripts'))
    from verify_sentinel_corrections import verify as independent_verify
    from verify_bar_order import projection
    export=root/'actual/export'; original=root/'actual/original/runtime'
    approval=read(root/'actual/execution_approval.json')
    start=read(root/'actual/execution_start.json');end=read(root/'actual/execution_end.json')
    final=read(root/'authorization/approval.final.json')
    assert approval==dict(final,start_at=approval['start_at'],stop_at=approval['stop_at']),'APPROVAL_BINDING'
    assert approval['status']=='APPROVED_BY_ONE' and approval['approval_reference'],'FINAL_APPROVAL'
    assert approval['source_hash']==SOURCE and approval['approved_git_head']==COMMIT,'APPROVED_SOURCE'
    assert start['identity']['source_hash']==SOURCE and start['identity']['commit']==COMMIT,'EXECUTED_SOURCE'
    source_hash=hashlib.sha256()
    for f in sorted((root/'source/one_sentinel').glob('*.py')):
        source_hash.update(f.name.encode());source_hash.update(f.read_bytes())
    assert source_hash.hexdigest()==SOURCE,'PACKAGED_EXECUTION_BYTES'
    assert sha((root/'authorization/launch_after_approval.py').read_bytes())==approval['launcher_sha256']==start['launcher_sha256'],'LAUNCHER_BYTES'
    assert approval['stop_at']-approval['start_at']==285 and start['started_at']==approval['start_at'],'TRIAL_WINDOW'
    assert end['elapsed_seconds']<=300 and end['within_300_seconds'],'RUNTIME_CAP'
    assert end['returncode']==0 and not end['forced_stop'],'UNCLEAN_STOP'
    rows=[json.loads(s) for s in (export/'ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    by=collections.defaultdict(list);index={}
    charges={};peak_charge=0
    for r in rows:
        b=json.loads(r['body']);kind=r['kind'];by[kind].append(b);index[kind,r['id']]=b
        if kind in ('http_intent','ws_intent'):
            charges[('h:' if kind=='http_intent' else 'w:')+r['id']]=b['read_reservation']
        elif kind=='receipt':
            context=b.get('context') or {}
            group='h:'+context['http_intent_id'] if context.get('http_intent_id') else 'w:'+context['ws_intent_id'] if context.get('ws_intent_id') else 'r:'+r['id']
            charges[group]=b['bytes']
        elif kind in ('http_result','ws_result'):
            charges[('h:' if kind=='http_result' else 'w:')+r['id']]=b['upper_bound_charge']
        peak_charge=max(peak_charge,sum(charges.values()))
    contract=by['contract'][0]
    assert contract['source_hash']==SOURCE,'CONTRACT_SOURCE'
    assert by['trial_approval']==[approval],'LEDGER_APPROVAL'
    for key in ('policy','account'):
        assert contract[key+'_hash']==approval[key+'_hash'],'CONTRACT_CONFIG'
        cfg=read(root/f'source/configs/sentinel_{key}_v1.json')
        assert cfg==contract[key],'PACKAGED_CONFIG'
    assert by['classification']==[dict(classification='REAL_FORWARD')],'REAL_ONLY'
    for r in by['receipt']:
        assert r['classification']=='REAL_FORWARD' and approval['start_at']<=r['received_at']<approval['stop_at'],'RECEIPT_WINDOW_OR_COHORT'
    for r in by['http_intent']+by['ws_intent']:
        assert approval['start_at']<=r['at']<approval['stop_at'],'REQUEST_OUTSIDE_WINDOW'
    http=by['http_intent'];book=sum(r['source']=='book' for r in http)
    assert len(http)<=approval['trial_http_attempt_cap']==12 and book<=approval['trial_book_attempt_cap']==6,'HTTP_CAP'
    for kind in ('http','ws'):
        intents={r['id'] for r in by[kind+'_intent']};results={r['id'] for r in by[kind+'_result']}
        assert intents==results,'UNSETTLED_TRANSPORT_INTENT'
    assert not by['analysis_attempt'] and not by['send_intent'] and not by['provider_receipt'],'FORBIDDEN_EXTERNAL_ACTION'
    assert not any(r.get('actual_delivery') for r in by['delivery']),'DELIVERY'
    assert all(r.get('actual_order') is False for r in by['fill']),'LIVE_ORDER'
    status=read(export/'status.json');meter=read(export/'meter.json')
    assert status['actual_orders']==status['actual_model_calls']==status['actual_deliveries']==0,'FORBIDDEN_STATUS'
    assert meter['rolling_24h_upper_bound_charge']<=approval['trial_received_byte_cap']==67108864,'BYTE_CAP'
    assert peak_charge<=67108864 and sum(charges.values())==meter['rolling_24h_upper_bound_charge'],'PREFIX_BYTE_CAP'
    reference=independent_verify(export)
    projected=projection(export)
    with sqlite3.connect((export/'observer.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)],'SQLITE_INTEGRITY'
    for name,expected in read(root/'actual/original_manifest.json').items():
        assert sha((root/'actual/original'/name).read_bytes())==expected,'ORIGINAL_CHANGED'
    rawfiles=list((export/'raw').glob('*.bin'))
    for f in rawfiles:assert sha(f.read_bytes())==f.stem,'RAW_FILENAME_HASH'
    states=collections.defaultdict(collections.Counter)
    for interpretation in by['interpretation']:
        for item in interpretation['items']:states[interpretation['source']][item['state']]+=1
    state_report={}
    for source,counter in states.items():
        n=sum(counter.values());three=sum(counter[s] for s in ('VALID','UNCERTAIN','FUTURE'))
        state_report[source]=dict(total=n,counts=dict(counter),ratios_all={s:counter[s]/n for s in ('VALID','UNCERTAIN','FUTURE','INVALID','UNVERIFIABLE')},
            three_state_denominator=three,ratios_three={s:counter[s]/three if three else None for s in ('VALID','UNCERTAIN','FUTURE')})
    counts=collections.Counter(r['source'] for r in by['receipt']);rawbytes=collections.Counter()
    for r in by['receipt']:rawbytes[r['source']]+=r['bytes']
    normalization=collections.defaultdict(list);timeline=[]
    for r in by['receipt']:
        applicable=[i for i in by['input'] if i.get('receipt_id',i['id'])==r['id']]
        timeline.append(dict(receipt_id=r['id'],source=r['source'],raw_sha256=r['sha256'],bytes=r['bytes'],
            received_at=r['received_at'],received_utc=utc(r['received_at']),original_processed_at=r['processed_at'],
            receipt_timing=r.get('timing'),normalized_inputs=[dict(input_id=i['id'],available_at=i['available_at'],
                processing_timing=i.get('processing_timing'),release_id=i.get('release_id')) for i in applicable]))
        for i in applicable:normalization[r['source']].append(i['available_at']-r['received_at'])
    calibrations=[];cfg=contract['policy']['clock_model']
    for c in by['clock_calibration']:
        calibrations.append(dict(id=c['id'],rtt_seconds=c['rtt_seconds'],received_at=c['received']['wall'],
            received_monotonic=c['received']['monotonic'],expires_monotonic=c['received']['monotonic']+cfg['max_age_seconds'],
            max_age_seconds=cfg['max_age_seconds'],session_id=c['received']['session_id'],offset_lower=c['offset_lower'],offset_upper=c['offset_upper']))
    initial_age=[]
    for r in by['interpretation']:
        if r.get('calibration_id'):
            c=index['clock_calibration',r['calibration_id']];receipt=index['receipt',r['receipt_id']]
            if receipt.get('timing') and receipt['timing']['session_id']==c['received']['session_id']:
                initial_age.append(receipt['timing']['monotonic']-c['received']['monotonic'])
    outcomes=collections.Counter((r['state'],r['reason']) for r in by['clock_pending_result'])
    release_wait=[r['wait_seconds'] for r in by['clock_pending_result'] if r['state']=='RELEASED' and r.get('wait_seconds') is not None]
    bar_anomalies=collections.Counter()
    for q in by['quarantine']:
        if index['receipt',q['id']]['source']=='bar':bar_anomalies['ordinary/'+q['error']]+=1
    for d in by['dedup']:
        if index['receipt',d['id']]['source']=='bar':bar_anomalies['ordinary/DUPLICATE_BAR']+=1
    for p in by['clock_pending_result']:
        if index['receipt',p['receipt_id']]['source']=='bar':bar_anomalies['pending/'+p['reason']]+=1
    latency=[]
    for d in by['decision']:
        for key in d['input_ids']:
            i=index['input',key];r=index['receipt',i.get('receipt_id',i['id'])]
            latency.append(dict(decision_id=d['id'],input_id=key,receipt_id=r['id'],receipt_to_decision=d['at']-r['received_at'],
                                availability_to_decision=d['at']-i['available_at']))
    return dict(status='PASS_REAL_PUBLIC_TRIAL_AUDIT',classification='REAL_FORWARD',source_hash=SOURCE,commit=COMMIT,
        window=dict(start_utc=utc(approval['start_at']),cutoff_utc=utc(approval['stop_at']),finished_utc=utc(end['finished_at']),
                    observation_limit_seconds=285,process_elapsed_seconds=end['elapsed_seconds'],forced_stop=end['forced_stop']),
        receipt_counts=dict(counts),receipt_bytes=dict(rawbytes),raw_files=len(rawfiles),meter=meter,
        raw_receipt_timeline=timeline,normalization_delay_seconds={s:stats(v) for s,v in normalization.items()},
        clock=dict(calibrations=calibrations,rtt_seconds=stats([c['rtt_seconds'] for c in calibrations]),initial_interpretation_age_seconds=stats(initial_age),rules=cfg),
        initial_time_states=state_report,pending=dict(created=len(by['clock_pending']),terminal=len(by['clock_pending_result']),
            active=len(by['clock_pending'])-len(by['clock_pending_result']),outcomes=[dict(state=k[0],reason=k[1],count=v) for k,v in sorted(outcomes.items())],release_wait_seconds=stats(release_wait)),
        bar=dict(receipts=counts['bar'],accepted_inputs=sum(i['source']=='bar' for i in by['input']),handling=dict(bar_anomalies),latest_projection=projected),
        decision_latency=latency,paper=dict(events=len(by['event']),decisions=len(by['decision']),
            action_reasons=dict(collections.Counter(r['action']+'/'+r['reason'] for r in by['decision'])),
            executions=len(by['execution']),execution_reasons=dict(collections.Counter(r['status']+'/'+str(r['reason']) for r in by['execution'])),
            fills=len(by['fill']),book_receipts=counts['book'],executable_book_observed=counts['book']>0,account=status),
        transport=dict(http_attempts=len(http),http_by_source=dict(collections.Counter(r['source'] for r in http)),book_attempts=book,
            http_statuses=dict(collections.Counter(str(r.get('http_status'))+'/'+r['status'] for r in by['http_result'])),
            ws_results=dict(collections.Counter(r['disposition'] for r in by['ws_result'])),source_gaps=by['source_gap'],stream_gaps=by['stream_gap'],halts=by['observation_halt'],
            unresolved_intents=0,peak_reserved_byte_charge=peak_charge),record_counts={k:len(v) for k,v in by.items() if v},independent_verifier=reference,
        limits=dict(actual_models=0,actual_orders=0,actual_notifications=0),
        limitations=['No packet/TLS byte measurement','No whole-function return/SQLite commit completion timestamps',
                     'No WS connection-attempt counter','No investment-edge or matured-return validation'],investment_edge_validated=False)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);p.add_argument('--out',type=Path);a=p.parse_args()
    result=verify(a.bundle.resolve());text=json.dumps(result,ensure_ascii=False,indent=2)
    if a.out:a.out.write_text(text,encoding='utf-8')
    else:print(text)
