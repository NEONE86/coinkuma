"""Executable, typed child hypotheses in the existing queue and authority.

Rules are a closed allowlist, never executable model-generated code. Previously
seen archives remain exploratory, irrespective of new spec registration time.
"""
import json,hashlib,math,time
from datetime import timedelta
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,process_lock
from .alpha_loop.worker import open_loop
from .alpha_loop.reference.research_loop import ExperimentSpec
from .batch_control import claim_owned,ack_preserving_overrun
from .batch_analysis import historical_contexts

ABSORPTION='SPOT_SELL_PRESSURE_ABSORPTION'
DELAY='CROSS_ASSET_INFORMATION_DELAY'
PANEL='REOBSERVE_COMPLETED_CAUSAL_PANEL'
RULES={ABSORPTION:dict(sell_share_min=.6,volume_ratio_min=1.5,absolute_hour_impact_max=.002),
       DELAY:dict(btc_return_min=.02,btc_volume_ratio_min=1.5,lag_residual_max=-.01),PANEL:{}}

def state(s,key,status,**details):
    rid=key+':'+status
    return s.get('hypothesis_state',rid,True) or s.append('hypothesis_state',rid,dict(id=rid,spec_id=key,status=status,at=utc(),**details))

def parent_record(s,key):
    return s.get('hypothesis_trial',key,True) or s.get('trial_result',key)

def propose(s,parent_id,mechanism=None):
    parent=parent_record(s,parent_id)
    prior=parent.get('mechanism_id')
    mechanism=mechanism or (PANEL if parent['status'] in ('DATA_INSUFFICIENT','EXECUTION_ERROR','EXPIRED') else ABSORPTION if prior is None else DELAY if prior==ABSORPTION else PANEL if prior==DELAY else ABSORPTION)
    if mechanism not in RULES:raise ValueError('UNSUPPORTED_MECHANISM')
    key='hyp-'+digest([parent_id,mechanism,'typed-v1'])[:24]
    old=s.get('hypothesis_spec',key,True)
    if old:return old
    from .economic_batches import implementation_hash
    source=parent.get('source_receipts')
    if source is None:raise ValueError('PARENT_SOURCE_BINDING_REQUIRED')
    available=max(r['received_at'] for rs in source.values() for r in rs)
    c=s.all('batch_config')[-1];now=utc()
    why={ABSORPTION:'Negative financing alone did not beat cash. Test actual aggressive spot selling with unusually small price impact, a different inventory-absorption mechanism; this does not prove forced liquidation.',
         DELAY:'The completed absorption exploration does not establish a deployable edge. Test delayed cross-asset transmission of BTC activity instead of assuming spot inventory absorption.',
         PANEL:'Two distinct mechanisms have been explored on used archives. Obtain a genuinely newly received panel before reobservation; do not inflate idea or independent-evidence counts.'}[mechanism]
    lineage='NEW_CAUSAL_HYPOTHESIS' if mechanism!=PANEL and not any(r['mechanism_id']==mechanism for r in s.all('hypothesis_spec')) else 'DATA_REOBSERVATION'
    content=dict(parent_trial_id=parent_id,parent_result_hash=digest(parent),mechanism_id=mechanism,change_reason=why,
        learned=dict(parent_status=parent['status'],parent_aggregates=parent.get('aggregates'),no_adoption=True),
        source_receipts=source,input_binding=dict(source_hash=digest(source),available_at=available,cutoff=now,
            missing_policy='DATA_INSUFFICIENT_NO_IMPUTATION',known_used_parent_archive=True),
        universe=['BTCUSDT','ETHUSDT','SOLUSDT'],selection_rule=dict(id=mechanism,parameters=RULES[mechanism]),
        comparison_arms=[] if mechanism==PANEL else ['PRICE_ONLY','ECONOMIC_RULE','AI_UNAVAILABLE','CASH'],
        ai=dict(status='NOT_REQUESTED',reason='No additional model invocation for this bounded repair study; existing paid decisions audited separately. A parent CASH answer is not a new child AI decision.',model=None,prompt_version=None,schema_hash=None),
        costs=dict(capital_usdt=10000,exposure_fraction=.125,fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1,
                   information_cost_per_call_usdt=.1,information_cost_status='SENSITIVITY_NOT_INVOICE',spot_funding_cashflow=0,accounting='FIXED_NOTIONAL_NOT_COMPOUNDED'),
        entry_delay_seconds=7200,horizon_seconds=86400,decision_validity_seconds=600,
        expiry=(instant(now)+timedelta(days=3)).isoformat(),invalidation=['Changed spec/source/model identity','Missing completed feature or label bars','Invalid taker/volume fields'],
        origin='HISTORICAL_EXPLORATION_REUSED_NOT_INDEPENDENT' if mechanism!=PANEL else 'DATA_COLLECTION_ONLY',
        lineage_class=lineage,version=1,registered_at=now,source_version=implementation_hash(),
        resource_limits=dict(trial_seconds=c['max_trial_seconds'],model_calls=0,cash_units=0,http_requests=0 if mechanism!=PANEL else 18),
        ready_at=(instant(now)+timedelta(hours=c['batch_interval_hours'])).isoformat() if mechanism==PANEL else now)
    spec=s.append('hypothesis_spec',key,dict(id=key,spec_hash=digest(content),**content))
    s.append('next_hypothesis',key,dict(id=key,spec_id=key,spec_hash=spec['spec_hash'],parent_trial=parent_id,change=why,due_at=spec['ready_at'],priority=1 if mechanism==PANEL else 2,selection_reason=why,queue_schema=2))
    state(s,key,'REGISTERED',spec_hash=spec['spec_hash']);return spec

def validate_spec(spec):
    content={k:v for k,v in spec.items() if k not in ('id','spec_hash','stored_at')}
    if digest(content)!=spec['spec_hash']:raise ValueError('SPEC_HASH')
    if spec['selection_rule']['id'] not in RULES or spec['selection_rule']['parameters']!=RULES[spec['mechanism_id']]:raise ValueError('UNREGISTERED_RULE_VARIANT')
    if digest(spec['source_receipts'])!=spec['input_binding']['source_hash']:raise ValueError('SPEC_SOURCE_IDENTITY')
    if instant(spec['input_binding']['available_at'])>instant(spec['registered_at']):raise ValueError('FUTURE_RECEIPT_IN_SPEC')
    if spec['ai']['status']!='NOT_REQUESTED' or spec['resource_limits']['model_calls']!=0:raise ValueError('UNSUPPORTED_AI_SPEC')
    return spec

def read_inputs(s,spec):
    validate_spec(spec);data={}
    for symbol,rs in spec['source_receipts'].items():
        if len(rs)!=2:raise ValueError('SOURCE_PAIR')
        decoded=[]
        for r in rs:
            path=s.root/'raw'/(r['sha256']+'.bin');raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('RAW_HASH')
            if instant(r['received_at'])>instant(spec['registered_at']):raise ValueError('AFTER_REGISTRATION_SOURCE_REBOUND')
            decoded.append(json.loads(raw))
        bars,funding=decoded
        if len({r[0] for r in bars})!=len(bars):raise ValueError('DUPLICATE_BAR')
        for r in bars:
            if len(r)<11 or r[6]!=r[0]+3599999:raise ValueError('BAR_INTERVAL')
            for idx in (1,2,3,4,5,7,9,10):
                if isinstance(r[idx],bool) or not math.isfinite(float(r[idx])) or float(r[idx])<0:raise ValueError('INVALID_BAR_VALUE')
            if min(float(r[i]) for i in (1,2,3,4))<=0 or float(r[10])>float(r[7])+1e-7:raise ValueError('TAKER_OR_PRICE')
            if r[6]>=instant(rs[0]['received_at']).timestamp()*1000:raise ValueError('UNFINISHED_BAR_AT_RECEIPT')
        data[symbol]=dict(bars=bars,funding=funding)
    if set(data)!=set(spec['universe']):raise ValueError('UNIVERSE_IDENTITY')
    return data

def features(data):
    contexts=historical_contexts(data);out=[]
    for ctx in contexts:
        row=dict(ctx,assets={s:dict(v) for s,v in ctx['assets'].items()})
        for symbol,values in row['assets'].items():
            bars={r[0]:r for r in data[symbol]['bars']};signal=ctx['signal_ms'];last=bars[signal-3600000]
            baseline=[float(bars[t][7]) for t in range(signal-25*3600000,signal-3600000,3600000)]
            q=float(last[7]);mean=sum(baseline)/len(baseline)
            if q<=0 or mean<=0:raise ValueError('MISSING_POSITIVE_VOLUME')
            values.update(aggressive_sell_share=1-float(last[10])/q,volume_ratio=q/mean,hour_return=float(last[4])/float(last[1])-1,
                          last_feature_close_ms=last[6],quote_volume=q,taker_buy_quote=float(last[10]))
        out.append(row)
    return out

def select(ctx,rule):
    ranked=sorted(ctx['assets'],key=lambda s:(ctx['assets'][s]['past_24h_return'],s));a=ctx['assets'];p=RULES[rule]
    if rule==ABSORPTION:
        candidates=[s for s in ranked if a[s]['past_24h_return']<0 and a[s]['aggressive_sell_share']>=p['sell_share_min'] and a[s]['volume_ratio']>=p['volume_ratio_min'] and abs(a[s]['hour_return'])<=p['absolute_hour_impact_max']]
    elif rule==DELAY:
        btc=a['BTCUSDT'];candidates=[]
        if btc['past_24h_return']>=p['btc_return_min'] and btc['volume_ratio']>=p['btc_volume_ratio_min']:
            candidates=sorted([s for s in ('ETHUSDT','SOLUSDT') if a[s]['past_24h_return']-btc['past_24h_return']<=p['lag_residual_max']],key=lambda s:(a[s]['past_24h_return']-btc['past_24h_return'],s))
    else:raise ValueError('UNKNOWN_SELECTION_RULE')
    return candidates[0] if candidates else None

def evaluate(s,spec):
    data=read_inputs(s,spec);contexts=features(data);prices={s:{b[0]:b for b in d['bars']} for s,d in data.items()};rows=[]
    cost=spec['costs'];rate=(cost['fee_bps_per_side']+cost['slippage_bps_per_side']+cost['assumed_half_spread_bps'])/10000
    for ctx in contexts:
        ranked=sorted(ctx['assets'],key=lambda s:(ctx['assets'][s]['past_24h_return'],s))
        choices={'PRICE_ONLY':ranked[0] if ctx['assets'][ranked[0]]['past_24h_return']<0 else None,'ECONOMIC_RULE':select(ctx,spec['mechanism_id']),'AI_UNAVAILABLE':None,'CASH':None}
        for arm,symbol in choices.items():
            if arm=='AI_UNAVAILABLE':rows.append(dict(case_id=ctx['id'],arm=arm,status='NOT_RUN',net_usdt=None,information_cost_usdt=0));continue
            gross=fee=qty=0.;entry=exit=None
            if symbol:
                entry=float(prices[symbol][ctx['entry_ms']][1]);exit=float(prices[symbol][ctx['exit_ms']][1]);qty=cost['capital_usdt']*cost['exposure_fraction']/(entry*(1+rate))
                gross=qty*(exit-entry);fee=qty*(entry+exit)*rate
            rows.append(dict(case_id=ctx['id'],arm=arm,status='HISTORICAL_RECONSTRUCTION',symbol=symbol,action='LONG' if symbol else 'NO_TRADE',
                entry_ms=ctx['entry_ms'],exit_ms=ctx['exit_ms'],entry_price=entry,exit_price=exit,quantity=qty,
                gross_usdt=gross,trading_cost_usdt=fee,information_cost_usdt=0,net_usdt=gross-fee,not_a_fill=True))
    aggregates={arm:dict(units=sum(r['arm']==arm and r['net_usdt'] is not None for r in rows),trades=sum(r['arm']==arm and bool(r.get('symbol')) for r in rows),
                        net_usdt=None if arm=='AI_UNAVAILABLE' else sum(r['net_usdt'] for r in rows if r['arm']==arm)) for arm in spec['comparison_arms']}
    economic=aggregates['ECONOMIC_RULE']['net_usdt'];price=aggregates['PRICE_ONLY']['net_usdt']
    verdict='REQUIRES_NEW_FORWARD_VALIDATION' if economic>max(0,price) else 'REJECTED_EXPLORATORY'
    return dict(status=verdict,mechanism_id=spec['mechanism_id'],spec_id=spec['id'],spec_hash=spec['spec_hash'],
                source_receipts=spec['source_receipts'],input_hash=digest(data),contexts=contexts,rows=rows,aggregates=aggregates,
                comparison_reason=f'Economic net {economic:.8f}, price net {price:.8f}, cash 0. Used historical data cannot validate forward edge.',
                counts=dict(historical_hypothesis_trials=1,historical_windows=len(contexts),historical_hypothetical_entries=sum(a['trades'] for a in aggregates.values()),
                            independent_forward_events=0,paper_fills=0,matured_forward_results=0,data_collection_runs=0,model_calls=0),
                historical_contamination=True,independent_validation=False,adopted=False)

def observe_panel(s,c,spec,request):
    from .economic_batches import BoundedInputs,SPOT,FUTURES
    inputs=BoundedInputs(s,request['id'],c);inputs.start_trial();sources={}
    end=int(instant(request['started_at']).timestamp()//3600)*3600000
    for symbol in spec['universe']:
        bars=inputs.get(SPOT,'/api/v3/klines',dict(symbol=symbol,interval='1h',startTime=end-32*86400000,endTime=end-1,limit=1000))
        funding=inputs.get(FUTURES,'/fapi/v1/fundingRate',dict(symbol=symbol,startTime=end-33*86400000,endTime=end-1,limit=1000))
        sources[symbol]=[bars['receipt'],funding['receipt']]
    return dict(status='DATA_OBSERVED',mechanism_id=PANEL,spec_id=spec['id'],spec_hash=spec['spec_hash'],source_receipts=sources,input_hash=digest(sources),
                counts=dict(historical_hypothesis_trials=0,historical_windows=0,historical_hypothetical_entries=0,independent_forward_events=0,paper_fills=0,matured_forward_results=0,data_collection_runs=1,model_calls=0),
                independent_validation=False,adopted=False,comparison_reason='New receipt only; overlap and prior archive use remain recorded.')

def run_queue(root,request_id=None,max_items=2):
    from .economic_batches import implementation_hash,readonly_rows
    root=Path(root).resolve()
    with process_lock(root):
        s=Store(root);loop=None
        try:
            s.verify();c=s.all('batch_config')[-1]
            if c['source_hash']!=implementation_hash():raise ValueError('SOURCE_VERSION_NOT_REGISTERED')
            if max_items<1 or max_items>c['max_trials_per_batch']:raise ValueError('TRIAL_LIMIT')
            mode='USER_REQUESTED_BOUNDED_FOLLOWUP' if request_id else 'SCHEDULED'
            unfinished=[r for r in s.all('research_request') if not s.get('research_request_result',r['id'],True)]
            request_id=request_id or (unfinished[0]['id'] if unfinished else 'queue-'+instant(utc()).date().isoformat())
            old=s.get('research_request_result',request_id,True)
            if old:return dict(status='CACHED',request_id=request_id,result=old,new_investment_trials=0)
            request=s.get('research_request',request_id,True)
            if not request:
                if (instant(utc())-instant(c['registered_at'])).days>=c['research_days']:return dict(status='RESEARCH_WINDOW_ENDED')
                if mode=='SCHEDULED':
                    starts=[x['started_at'] for x in s.all('research_request')]+[x['started_at'] for x in s.all('research_batch')]
                    if starts and (instant(utc())-max(map(instant,starts))).total_seconds()<c['batch_interval_hours']*3600:return dict(status='NOT_DUE')
                request=s.append('research_request',request_id,dict(id=request_id,mode=mode,started_at=utc(),max_items=max_items,
                    source_version=implementation_hash(),config_hash=digest(c),resource_caps=dict(max_trials=c['max_trials_per_batch'],max_seconds=c['max_batch_seconds'],max_model_calls=0,new_cash_units=0),
                    authorization='Explicit user follow-up after PR12 review' if mode!='SCHEDULED' else 'Existing daily paper research cadence',
                    cadence_note='Explicit user-triggered follow-up counted separately; does not change or reset shared budgets or automatic daily interval.'))
                s.append('research_batch',request_id,dict(id=request_id,started_at=request['started_at'],kind='TYPED_HYPOTHESIS_REQUEST',config_hash=digest(c)))
            if not s.all('hypothesis_spec'):
                parents=[r for r in s.all('trial_result') if r.get('family')=='funding_crowding' and 'aggregates' in r]
                if not parents:raise ValueError('ACTUAL_PARENT_RESULT_REQUIRED')
                propose(s,parents[0]['id'])
            # Crash after the completed result but before successor registration.
            # Reconcile this transition before excluding finished work from the queue.
            for trial in s.all('hypothesis_trial'):
                successor=propose(s,trial['id'])
                state(s,trial['id'],'SUCCESSOR_REGISTERED',child_spec_id=successor['id'])
            alpha=Path(c['alpha_runtime']);protocol=readonly_rows(alpha,'alpha_protocol')[0];completed=[];started=time.monotonic()
            already=[r for r in s.all('hypothesis_trial') if r['request_id']==request_id]
            used=sum(r['elapsed_seconds'] for r in s.all('hypothesis_receipt') if r['request_id']==request_id)
            for _ in range(max(0,request['max_items']-len(already))):
                candidates=[q for q in s.all('next_hypothesis') if q.get('spec_id') and not s.get('hypothesis_trial',q['spec_id'],True) and instant(q['due_at'])<=instant(utc())]
                if not candidates:break
                q=min(candidates,key=lambda x:(x['priority'],x['due_at'],x['id']));spec=validate_spec(s.get('hypothesis_spec',q['spec_id']))
                if spec['source_version']!=implementation_hash():return dict(status='FROZEN_SPEC_REQUIRES_ORIGINAL_EXECUTOR',spec_id=spec['id'])
                if c['max_batch_seconds']-(time.monotonic()-started+used)<c['max_trial_seconds']:break
                if instant(utc())>instant(spec['expiry']):
                    result=s.append('hypothesis_trial',spec['id'],dict(id=spec['id'],request_id=request_id,parent_trial_id=spec['parent_trial_id'],source_version=spec['source_version'],
                        completed_at=utc(),status='EXPIRED',spec_id=spec['id'],spec_hash=spec['spec_hash'],mechanism_id=spec['mechanism_id'],source_receipts=spec['source_receipts'],counts={},
                        resource_accounting='NO_EXECUTION_NO_NEW_RESERVATION'))
                    state(s,spec['id'],'COMPLETED',result_hash=digest(result),verdict=result['status']);propose(s,result['id']);continue
                with process_lock(alpha):
                    loop=open_loop(alpha,protocol['config']);key='child:'+spec['id'];eid=key
                    if not loop.db.execute('SELECT 1 FROM experiments WHERE id=?',(eid,)).fetchone():
                        loop.register_experiment(ExperimentSpec(eid,spec['mechanism_id']+'/'+spec['id'],'CASHFLOW',spec['version'],spec['registered_at'],spec['expiry'],0,c['max_trial_seconds'],'HISTORICAL'))
                        loop.record_data(eid,spec['spec_hash']);loop.enqueue_task(key,eid,'research',input_hash=spec['spec_hash'],cash_units=0,time_seconds=c['max_trial_seconds'])
                    task=loop.get_task(key);recovery=task['state']=='CLAIMED'
                    if task.get('overrun_receipt_json'):return dict(status='RECONCILIATION_REQUIRED',task_key=key,reservation_retained=True)
                    if task['state']=='QUEUED':
                        if loop.db.execute('SELECT state FROM experiments WHERE id=?',(eid,)).fetchone()[0]=='PAUSED':loop.resume(eid)
                        task=claim_owned(loop,[key])
                        if task is None:
                            loop.suspend(eid,'PAUSED','Existing shared budget/halt prevents child research; maintenance remains separate.')
                            return dict(status='BUDGET_BLOCKED',request_id=request_id)
                    state(s,spec['id'],'CLAIMED',request_id=request_id,task_key=key)
                    receipt=s.get('hypothesis_receipt',spec['id'],True)
                    if not receipt:
                        at=time.monotonic()
                        try:
                            if instant(utc())>instant(spec['expiry']):result=dict(status='EXPIRED',source_receipts=spec['source_receipts'],counts={},mechanism_id=spec['mechanism_id'])
                            else:result=observe_panel(s,c,spec,request) if spec['mechanism_id']==PANEL else evaluate(s,spec)
                        except (ValueError,KeyError,FileNotFoundError) as exc:result=dict(status='DATA_INSUFFICIENT',error=str(exc),mechanism_id=spec['mechanism_id'],source_receipts=spec['source_receipts'],counts={})
                        except Exception as exc:result=dict(status='EXECUTION_ERROR',error=str(exc),mechanism_id=spec['mechanism_id'],source_receipts=spec['source_receipts'],counts={})
                        elapsed=math.ceil(time.monotonic()-at)
                        if recovery:elapsed=max(elapsed,c['max_trial_seconds'])
                        receipt=s.append('hypothesis_receipt',spec['id'],dict(id=spec['id'],request_id=request_id,result=result,input_hash=spec['spec_hash'],output_hash=digest(result),elapsed_seconds=elapsed,
                            time_accounting='FULL_RESERVATION_UNCERTAIN_INTERRUPTION' if recovery else 'MEASURED_SECONDS',new_paid_cash_units=0))
                    account=ack_preserving_overrun(loop,task,receipt['result'],receipt['elapsed_seconds'])
                    if account['status']!='ACKNOWLEDGED':return account
                    result=s.get('hypothesis_trial',spec['id'],True) or s.append('hypothesis_trial',spec['id'],dict(id=spec['id'],request_id=request_id,parent_trial_id=spec['parent_trial_id'],
                        source_version=spec['source_version'],completed_at=utc(),**receipt['result']))
                    state(s,spec['id'],'COMPLETED',result_hash=digest(result),verdict=result['status'])
                    successor=propose(s,result['id'])
                    state(s,spec['id'],'SUCCESSOR_REGISTERED',child_spec_id=successor['id'])
                    completed.append(result);loop.close();loop=None
            trials=[r for r in s.all('hypothesis_trial') if r['request_id']==request_id]
            counts={k:sum(r.get('counts',{}).get(k,0) for r in trials) for k in ('historical_hypothesis_trials','historical_windows','historical_hypothetical_entries','independent_forward_events','paper_fills','matured_forward_results','data_collection_runs','model_calls')}
            counts['new_causal_hypotheses']=sum(s.get('hypothesis_spec',r['id'])['lineage_class']=='NEW_CAUSAL_HYPOTHESIS' for r in trials)
            counts.update(numerical_variants=0,data_reobservations=sum(s.get('hypothesis_spec',r['id'])['lineage_class']=='DATA_REOBSERVATION' for r in trials),cache_reexecutions=0)
            result=s.append('research_request_result',request_id,dict(id=request_id,completed_at=utc(),trial_ids=[r['id'] for r in trials],counts=counts,
                statuses={status:sum(r['status']==status for r in trials) for status in sorted({r['status'] for r in trials})},
                next_queue=[q['spec_id'] for q in s.all('next_hypothesis') if q.get('spec_id') and not s.get('hypothesis_trial',q['spec_id'],True)],
                new_global_alpha_allocations=0,edge_proven=False))
            s.append('batch_result',request_id,dict(id=request_id,kind='TYPED_HYPOTHESIS_REQUEST',result_id=request_id,completed_at=utc()))
            return result
        finally:
            if loop:loop.close()
            s.close()
