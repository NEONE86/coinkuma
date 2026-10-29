"""Finite forward receipt pilot inside the existing scheduler and shared caps.

Measures observability/latency, never manufactures economic labels or fills.
"""
import argparse,json
from datetime import timedelta
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,process_lock
from .autonomy.market import SPOT,FUTURES,closed_bars,book_view
from .historical_expansion import H,SYMBOLS,ms,iso,price,ABSORPTION,DELAY,select
from .expansion_io import Inputs,owned,version

class PilotInputs(Inputs):
    def __init__(self,*args,deadline,**kwargs):
        super().__init__(*args,**kwargs);self.deadline=deadline
    def check(self):
        # Check every attempt, not only the beginning of a multi-request job.
        if ms(utc())>=self.deadline:raise ValueError('ABSOLUTE_COLLECTION_DEADLINE')
        super().check()

def register(s,now=None,revision=False):
    now=now or utc();key='receipt-pilot-v2';old=s.get('receipt_pilot_spec',key,True)
    if old:
        if not revision:return old
        old=s.all('receipt_pilot_spec')[-1]
        if old['source_version']==version('pilot'):return old
        key='receipt-pilot-source-'+version('pilot')[:12];new=s.get('receipt_pilot_spec',key,True)
        if new:return new
        if any(r['pilot_id']==old['id'] for r in s.all('pilot_observation')) or ms(now)>=ms(old['start_signal_at']):raise ValueError('SOURCE_ONLY_REVISION_REQUIRES_UNSTARTED_PILOT')
        body={k:v for k,v in old.items() if k not in ('id','stored_at')}
        body.update(id=key,registered_at=now,source_version=version('pilot'),operational_predecessor=old['id'],revision_reason='Reconcile durable acknowledgements before expiry guards and enforce byte/absolute-clock caps; clocks, costs, cadence and economic rules unchanged.')
        new=s.append('receipt_pilot_spec',key,body)
        s.append('pilot_stop',old['id'],dict(id=old['id'],at=now,reason='SUPERSEDED_SOURCE_ONLY_BEFORE_FIRST_SLOT',replacement_id=key))
        return new
    c=s.all('batch_config')[-1];start=(instant(now).replace(minute=0,second=0,microsecond=0)+timedelta(hours=1))
    stop=min(start+timedelta(days=14),instant(c['registered_at'])+timedelta(days=c['research_days']))
    if start>=stop:raise ValueError('EXISTING_RESEARCH_WINDOW_ENDED')
    parents=s.all('event_followup_design')
    for p in parents:
        if instant(now)>instant(p['first_feature_receipt_due']):
            s.append('followup_missed_start',p['id'],dict(id=p['id'],at=now,original_start=p['start_signal_at'],status='MISSED_NOT_RETROACTIVE',original_hash=digest(p)))
    spec=s.append('receipt_pilot_spec',key,dict(id=key,registered_at=now,source_version=version('pilot'),start_signal_at=start.isoformat(),stop_at=stop.isoformat(),deadline=stop.isoformat(),
        purpose='RECEIPT_EXECUTION_FEASIBILITY_NOT_PROFITABILITY_COHORT',auto_execution=True,scheduler='EXISTING_TASK_ONLY_NO_NEW_SCHEDULE',interval_hours=4,feature_deadline_seconds=300,quote_deadline_seconds=300,
        entry_delay_hours=2,horizons_hours=[4,24],lockout_hours=26,universe=list(SYMBOLS),rules='EXACT_PARENT_GATES',parents=[dict(id=p['id'],sha256=digest(p)) for p in parents],
        resource_caps=dict(max_attempts_rolling_24h=c['max_http_requests_per_batch'],max_seconds_per_job=100,max_bytes_per_job=2*1024*1024,model_calls=0,cash_units=0),
        budget_design='Six feature slots/day *6 requests=36 plus worst-case 18 quote jobs/day *3=54. All retries and other batch HTTP also consume the same rolling90 cap; cap exhaustion is missing, never extra spending.',
        clocks='Feature bar closes strictly before signal; public funding timestamp <= signal; actual receipt and decision after signal and <=5min. Original 2min design remains missed; this separately versioned cadence/5min feasibility window fits unchanged 2min scheduler.',
        stop_rule='No request at or after stop; incomplete pending quotes become MISSING_STOP. End is capped at original research deadline, never extends the original authority window.',
        labels='Quotes are actual received books; no fill/PnL/profitability labels in this feasibility pilot; late or absent quotes are explicit missing.'))
    return spec

def slots(spec):
    return range(ms(spec['start_signal_at']),ms(spec['stop_at']),spec['interval_hours']*H)

def features(s,inp,t):
    assets={};bindings=[]
    for symbol in SYMBOLS:
        rb=inp.get(SPOT,'/api/v3/klines',dict(symbol=symbol,interval='1h',startTime=t-25*H,endTime=t-1,limit=25))
        rf=inp.get(FUTURES,'/fapi/v1/fundingRate',dict(symbol=symbol,startTime=t-32*H,endTime=t,limit=1000))
        bars=closed_bars(json.loads((s.root/'raw'/(rb['sha256']+'.bin')).read_bytes()),iso(t),rb['received_at'])
        fs=json.loads((s.root/'raw'/(rf['sha256']+'.bin')).read_bytes());fs=sorted([f for f in fs if f['fundingTime']<=t],key=lambda f:f['fundingTime'])
        if len(fs)<3 or t-fs[-1]['fundingTime']>12*H:raise ValueError('MISSING_FUNDING_COVERAGE')
        last=bars[-1];q=float(last[7]);mean=sum(float(b[7]) for b in bars[:-1])/24
        if min(q,mean)<=0:raise ValueError('MISSING_VOLUME')
        assets[symbol]=dict(past_24h_return=float(last[4])/float(bars[1][1])-1,aggressive_sell_share=1-float(last[10])/q,volume_ratio=q/mean,hour_return=float(last[4])/float(last[1])-1,last_feature_close_ms=last[6],quote_volume=q,taker_buy_quote=float(last[10]),funding_source_time=fs[-1]['fundingTime'])
        bindings.extend([dict(symbol=symbol,kind='bars',receipt=rb),dict(symbol=symbol,kind='funding',receipt=rf)])
    return dict(assets=assets,bindings=bindings,signal_at=iso(t),received_at=max(r['receipt']['received_at'] for r in bindings),decision_at=utc())

def jobs(s,spec):
    out=[]
    for t in slots(spec):out.append(dict(id=spec['id']+':feature:'+str(t),kind='FEATURE',signal_ms=t,due_ms=t,deadline_ms=t+spec['feature_deadline_seconds']*1000))
    # Durable feature decisions regenerate exactly the same pending quote jobs
    # after a process restart, without another feature fetch or changed decision.
    for row in s.all('pilot_observation'):
        if row.get('pilot_id')!=spec['id'] or row.get('status')!='VALID_SIGNAL':continue
        t=row['signal_ms']
        for label,h in (('ENTRY',2),('EXIT_4H',6),('EXIT_24H',26)):
            out.append(dict(id=spec['id']+':'+label+':'+str(t),kind=label,signal_ms=t,due_ms=t+h*H,deadline_ms=t+h*H+spec['quote_deadline_seconds']*1000,feature_id=row['id']))
    return sorted(out,key=lambda j:(j['due_ms'],j['id']))

def summarize(s,spec,at):
    records=[r for r in s.all('pilot_observation') if r['pilot_id']==spec['id']];pending=[j for j in jobs(s,spec) if not s.get('pilot_observation',j['id'],True)]
    stop=s.get('pilot_stop',spec['id'],True)
    return dict(status='STOPPED' if stop else 'ACTIVE_WAITING_NEXT_DUE',pilot_id=spec['id'],at=at,start=spec['start_signal_at'],stop_at=spec['stop_at'],next_due=iso(pending[0]['due_ms']) if pending and not stop else None,
        feature_slots_received=sum(r['kind']=='FEATURE' and r['status'] in ('VALID_SIGNAL','NO_SIGNAL') for r in records),signal_slots=sum(r['status']=='VALID_SIGNAL' for r in records),quote_jobs_received=sum(r['status']=='BOOKS_RECEIVED' for r in records),missing=sum(r['status'].startswith('MISSING') for r in records),paper_fills=0,profitability_outcomes=0,stop=stop)

def service(root):
    with process_lock(root):
        s=Store(root)
        try:
            specs=s.all('receipt_pilot_spec')
            if not specs:return dict(status='NOT_REGISTERED')
            spec=specs[-1];now=utc();t=ms(now)
            if spec['source_version']!=version('pilot'):raise ValueError('PILOT_SOURCE_CHANGED')
            if s.get('pilot_stop',spec['id'],True):return summarize(s,spec,now)
            stop=t>=ms(spec['stop_at'])
            for job in jobs(s,spec):
                if s.get('pilot_observation',job['id'],True):continue
                if t<job['due_ms'] and not stop:break
                if stop or t>job['deadline_ms']:
                    s.append('pilot_observation',job['id'],dict(id=job['id'],pilot_id=spec['id'],**{k:v for k,v in job.items() if k!='id'},status='MISSING_STOP' if stop else 'MISSING_DEADLINE',recorded_at=now,new_http_attempts=0));continue
                operation=dict(id=job['id'],registered_at=spec['registered_at'],deadline=iso(min(job['deadline_ms'],ms(spec['stop_at']))),pilot_hash=digest(spec),job=job)
                def execute(started):
                    inp=PilotInputs(s,job['id'],started,100,max_bytes=2*1024*1024,rolling=True,deadline=min(job['deadline_ms'],ms(spec['stop_at'])))
                    if job['kind']=='FEATURE':
                        f=features(s,inp,job['signal_ms'])
                        if ms(f['decision_at'])>job['deadline_ms']:return dict(status='MISSING_LATE_FEATURE',**f)
                        selected={m:select(f,m) for m in (ABSORPTION,DELAY)};accepted={}
                        for m,symbol in selected.items():
                            old=[r for r in s.all('pilot_observation') if r.get('pilot_id')==spec['id'] and r.get('accepted',{}).get(m)]
                            blocked=any(job['signal_ms']<r['signal_ms']+26*H for r in old)
                            if symbol and not blocked:accepted[m]=symbol
                        return dict(status='VALID_SIGNAL' if accepted else 'NO_SIGNAL',selected=selected,accepted=accepted,price_selector=price(f),**f)
                    books=[]
                    for symbol in SYMBOLS:
                        r=inp.get(SPOT,'/api/v3/depth',dict(symbol=symbol,limit=20));data=json.loads((s.root/'raw'/(r['sha256']+'.bin')).read_bytes());book=book_view(data,r)
                        bid=float(data['bids'][0][0]);ask=float(data['asks'][0][0])
                        books.append(dict(symbol=symbol,receipt=r,bid=bid,ask=ask,spread_bps=(ask-bid)/((ask+bid)/2)*10000,latency_seconds=(ms(r['received_at'])-job['due_ms'])/1000,book=book))
                    return dict(status='BOOKS_RECEIVED' if ms(utc())<=job['deadline_ms'] else 'MISSING_LATE_QUOTE',books=books)
                receipt=owned(s,job['id'],operation,100,execute)
                if receipt.get('status')=='BUDGET_BLOCKED':break
                result=receipt['result']
                if result.get('status')=='EXECUTION_ERROR':result=dict(result,status='MISSING_EXECUTION_ERROR')
                s.append('pilot_observation',job['id'],dict(id=job['id'],pilot_id=spec['id'],**{k:v for k,v in job.items() if k!='id'},**result,operation_hash=digest(receipt),recorded_at=utc()))
                break # one bounded job per existing scheduler invocation
            if stop:s.append('pilot_stop',spec['id'],dict(id=spec['id'],at=now,reason='FROZEN_DEADLINE_REACHED'))
            return summarize(s,spec,utc())
        finally:s.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('action',choices=['register','revise-unstarted','service','status','stop']);a=p.parse_args()
    if a.action=='service':r=service(a.runtime)
    else:
        with process_lock(a.runtime):
            s=Store(a.runtime)
            try:
                if a.action in ('register','revise-unstarted'):r=register(s,revision=a.action=='revise-unstarted')
                else:
                    spec=s.all('receipt_pilot_spec')[-1]
                    if a.action=='stop':s.append('pilot_stop',spec['id'],dict(id=spec['id'],at=utc(),reason='EXPLICIT_STOP'))
                    r=summarize(s,spec,utc())
            finally:s.close()
    print(json.dumps(r,ensure_ascii=False,indent=2))
