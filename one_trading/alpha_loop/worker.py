"""One authoritative controller and actual public-data worker; paper only."""
import json, math, os, time, uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from one_trading.autonomy.store import Store,utc,instant,digest,canonical,atomic_bytes,process_lock,require_disk
from .protocol import defaults,source_hash
from .observations import capture_flow,collect_snapshot,economic_label,state_at
from .decisions import price_decision,model_decision,retrieve_memory
from .trading import attempt_entry,maintain_exits,books_now
from .reference.research_loop import ResearchLoop,ExperimentSpec
from .reference.evidence import EvidenceSpec,EvidenceLedger,Metrics
from .reference.integration import bind_and_activate,submit_calculated_evidence

CLAIMS={'semantic':('CF_SEMANTIC',None),'memory':('CF_SEMANTIC_MEMORY','CF_SEMANTIC')}

def initialize(root,authority_root=None):
    root=Path(root).resolve()
    with process_lock(root):
        store=Store(root)
        try:
            old=store.get('alpha_protocol','frozen',True)
            if old:return old
            if (root/'authority.json').exists():raise ValueError('EXISTING_AUTHORITY_WITHOUT_PROTOCOL')
            config=defaults();now=utc();identity=uuid.uuid4().hex;cohort='v'+str(config['version'])+'-'+uuid.uuid4().hex[:8]
            database=root/'authority.sqlite3';parent=None
            if authority_root is not None:
                parent=Path(authority_root).resolve()
                original=json.loads((parent/'authority.json').read_text(encoding='utf-8'))
                database=Path(original.get('database_path',str(parent/original['database']))).resolve()
                if not database.is_file():raise ValueError('EXISTING_GLOBAL_AUTHORITY_REQUIRED')
                identity=original['id']
            protocol=store.append('alpha_protocol','frozen',dict(id='frozen',config=config,
                 config_hash=digest(config),implementation_hash=source_hash(),registered_at=now,
                 new_case_deadline=(instant(now)+timedelta(days=config['research_days'])).isoformat(),
                 authority_id=identity,authoritative_runtime=str(root),mode='FORWARD_PAPER',
                 cohort_id=cohort,authority_database=str(database),parent_authority_runtime=str(parent) if parent else None))
            atomic_bytes(root/'authority.json',canonical(dict(id=identity,runtime=str(root),
                         database='authority.sqlite3',database_path=str(database),cohort_id=cohort,protocol_hash=digest(protocol))))
            loop=open_loop(root,config);loop.close()
            return protocol
        finally:store.close()

def open_loop(root,config):
    root=Path(root);a=json.loads((root/'authority.json').read_text(encoding='utf-8')) if (root/'authority.json').exists() else {}
    database=Path(a.get('database_path',str(root/'authority.sqlite3')))
    return ResearchLoop(database,{
        'CASHFLOW':{'cash_units':0,'time_seconds':config['research_time_budget_seconds']},
        'CAPITAL_GROWTH':{'cash_units':0,'time_seconds':7200}})

def verify_authority(store):
    p=store.get('alpha_protocol','frozen');a=json.loads((store.root/'authority.json').read_text(encoding='utf-8'))
    if a['id']!=p['authority_id'] or Path(a['runtime']).resolve()!=store.root or p['authoritative_runtime']!=str(store.root):
        raise ValueError('COPIED_OR_DIFFERENT_AUTHORITY_FORBIDDEN')
    if a['protocol_hash']!=digest(p) or p['config_hash']!=digest(p['config']):raise ValueError('AUTHORITY_HASH')
    if a.get('database_path',str(store.root/'authority.sqlite3'))!=p.get('authority_database',str(store.root/'authority.sqlite3')):raise ValueError('AUTHORITY_DATABASE_CHANGED')
    if p['implementation_hash']!=source_hash():raise ValueError('SOURCE_CHANGED_FROZEN_COHORT')
    store.verify();require_disk(store.root)
    return p

def claim_id(store,label):
    return 'alpha-cf-'+store.get('alpha_protocol','frozen').get('cohort_id','v1')+'-'+label

def activate(loop,store,protocol,snapshot):
    config=protocol['config']
    for label,(own,memory) in CLAIMS.items():
        if label=='memory' and not retrieve_memory(store,snapshot['cutoff']):continue
        eid=claim_id(store,label)
        old=loop.db.execute('SELECT id FROM experiments WHERE id=?',(eid,)).fetchone()
        if not old:
            version=loop.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM experiments WHERE lineage=?',('CF_RESIDUAL_FORCED_FLOW/'+label,)).fetchone()[0]
            loop.register_experiment(ExperimentSpec(eid,'CF_RESIDUAL_FORCED_FLOW/'+label,'CASHFLOW',version,
                protocol['registered_at'],(instant(protocol['new_case_deadline'])+timedelta(days=2)).isoformat(),
                0,config['research_time_budget_seconds'],'FORWARD_PAPER'))
            loop.record_data(eid,digest(snapshot))
        if not store.get('alpha_evidence_spec',eid,True):
            spec=bind_and_activate(loop,eid,policy=own,baseline='CF_PRICE_ONLY',memory_comparator=memory,
                 risk_fingerprint=digest(config),return_scale=config['return_scale'],minimum_clusters=config['minimum_clusters'],
                 max_unit_loss=config['max_unit_loss'],decision_contract='PRESTART_RULE_AND_INPUT_NATIVE_OUTPUT_LATER')
            store.append('alpha_evidence_spec',eid,dict(id=eid,spec=asdict(spec)))

def evidence_ledger(store,eid):
    x=dict(store.get('alpha_evidence_spec',eid)['spec']);x['lambda_grid']=tuple(x['lambda_grid'])
    ledger=EvidenceLedger(EvidenceSpec(**x))
    eligible=set()
    for case in store.all('alpha_case'):
        if instant(case['registered_at'])<=instant(ledger.spec.registered_at):continue
        ledger.register_case(case_id=case['id'],cluster_id=case['id'],decision_at=case['registered_at'],
            available_at=case['cutoff'],starts_at=case['starts_at'],due_at=case['due_at'],
            decision_hashes={p:case['policy_commitments'][p] for p in ledger.spec.policies},
            risk_fingerprint=ledger.spec.risk_fingerprint,spec_hash=ledger.spec.fingerprint)
        eligible.add(case['id'])
    outcomes=sorted(store.all('alpha_outcome'),key=lambda r:r['received_at'])
    for o in outcomes:
        if o['case_id'] not in eligible:continue
        if any(o['native'][p]['metrics'] is None for p in ledger.spec.policies):continue
        ledger.resolve(case_id=o['case_id'],metrics={p:Metrics(**o['native'][p]['metrics']) for p in ledger.spec.policies},
                       received_at=o['received_at'],as_of=o['received_at'],source_hash=digest(o),mode='FORWARD_PAPER')
    return ledger

def submit(store,loop):
    receipts={}
    for label in CLAIMS:
        eid=claim_id(store,label)
        if store.get('alpha_evidence_spec',eid,True):
            receipts[label]=submit_calculated_evidence(loop,evidence_ledger(store,eid))['receipt']
    return receipts

def new_case(store,loop,protocol,key):
    config=protocol['config'];case=store.get('alpha_case',key,True)
    if case:return advance(store,config,case)
    flow=capture_flow(store,config,key+':initial')
    snapshot=collect_snapshot(store,config,key,flow)
    if snapshot['errors']:
        return dict(status='DATA_BLOCKED',errors=snapshot['errors'],snapshot_hash=digest(snapshot))
    screen=store.append('alpha_numeric_screen',key,dict(id=key,**price_decision(snapshot,config),
        scope='FULL_UNIVERSE_PRE_REGISTRATION_SCREEN_NOT_EXECUTED_RETURN'))
    if not flow['events'] or flow['coverage']!='CONTIGUOUS_SUBSCRIBED_WINDOW':
        return dict(status='DATA_BLOCKED_NO_QUALIFIED_CAUSE_WINDOW',snapshot_hash=digest(snapshot),
                    numeric_screen_hash=digest(screen),formal_case_registered=False)
    activate(loop,store,protocol,snapshot)
    now=utc();start=(instant(now)+timedelta(seconds=1)).isoformat()
    commitments={arm:digest(dict(arm=arm,config_hash=protocol['config_hash'],source_hash=protocol['implementation_hash'],
                    input_hash=digest(snapshot),flow_hash=digest(flow),memory_hash=digest(retrieve_memory(store,snapshot['cutoff']))
                    if arm=='CF_SEMANTIC_MEMORY' else None)) for arm in config['arms']}
    case=store.append('alpha_case',key,dict(id=key,cutoff=snapshot['cutoff'],registered_at=now,starts_at=start,
             due_at=(instant(start)+timedelta(seconds=config['horizon_seconds'])).isoformat(),
             missing_deadline=(instant(start)+timedelta(seconds=config['horizon_seconds']+config['missing_grace_seconds'])).isoformat(),
             snapshot_id=key,flow_window_id=flow['id'],policy_commitments=commitments,
             config_hash=protocol['config_hash'],implementation_hash=protocol['implementation_hash'],
             economic_question=config['economic_predicate'],counter_hypothesis=config['counter_hypothesis']))
    # Commit the entire nonoverlapping case to the authoritative alpha registry BEFORE action.
    submit(store,loop)
    return advance(store,config,case)

def advance(store,config,case):
    snapshot=store.get('alpha_snapshot',case['snapshot_id']);flow=store.get('flow_window',case['flow_window_id'])
    for arm in config['arms']:
        key=case['id']+':'+arm;decision=store.get('alpha_decision',key,True)
        if not decision:
            if arm=='CF_PRICE_ONLY':
                decision=store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,model_call_ids=[],**price_decision(snapshot,config)))
            elif arm=='CF_CASH':
                decision=store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,status='DECIDED',action='NO_TRADE',
                       symbol='CASH',ready_at=utc(),input_hash=digest(snapshot),model_call_ids=[]))
            else:decision=model_decision(store,config,case,arm,snapshot,flow)
        if decision:
            # The baseline executes BEFORE the slower model is started.
            eligible=max(instant(case['starts_at']),instant(decision['ready_at'])+timedelta(seconds=config['execution_delay_seconds']))
            wait=(eligible-instant(utc())).total_seconds()
            if decision['action']=='LONG' and 0<wait<=3:time.sleep(wait)
            attempt_entry(store,config,case,snapshot,decision,eligible_at=eligible.isoformat())
    decisions=[store.get('alpha_decision',case['id']+':'+a,True) for a in config['arms']]
    if all(decisions):
        matched=store.get('alpha_matched_context',case['id'],True)
        if not matched:
            eligible=max(instant(d['ready_at']) for d in decisions)+timedelta(seconds=config['execution_delay_seconds'])
            wait=(eligible-instant(utc())).total_seconds()
            if 0<wait<=3:time.sleep(wait)
            books,errors=books_now(store,config['universe'])
            if not errors:
                matched=store.append('alpha_matched_context',case['id'],dict(id=case['id'],eligible_at=eligible.isoformat(),books=books))
        if matched:
            for d in decisions:attempt_entry(store,config,case,snapshot,d,track='MATCHED',eligible_at=matched['eligible_at'],observed_books=matched['books'])
    maintain_exits(store,config,case,snapshot)
    return dict(status='WAITING_MATURITY',case_id=case['id'],due_at=case['due_at'])

def metrics_for(store,config,case,arm,track):
    key=case['id']+':'+arm+':'+track
    decision=store.get('alpha_decision',case['id']+':'+arm,True)
    entry=store.get('alpha_entry_result',key,True)
    if not decision or decision['status']!='DECIDED' or not entry:
        return dict(status='UNAVAILABLE',metrics=None,reason=decision['status'] if decision else 'DECISION_MISSING')
    information=len(decision['model_call_ids'])*config['information_cost_usdt_per_model_call']/config['capital_usdt']
    if entry['status']=='DATA_BLOCKED':return dict(status='DATA_BLOCKED',metrics=None,reason=entry['reason'])
    if entry['status']!='FILLED':
        return dict(status='RESOLVED',metrics=dict(gross_return=0.,trading_cost=0.,information_cost=information),
                    net_return=-information,executed=False,entry_status=entry['status'])
    exit=store.get('alpha_exit',key,True)
    if not exit:return dict(status='OPEN_OR_MISSING_EXIT',metrics=None)
    if instant(exit['fill']['quote_received_at'])>instant(case['due_at'])+timedelta(seconds=config['missing_grace_seconds']):
        return dict(status='INVALID_ENDPOINT_DELAY',metrics=None)
    a=entry['fill'];b=exit['fill'];capital=config['capital_usdt']
    gross=a['quantity']*(b['mid']-a['mid'])/capital
    cost=sum(x[k] for x in (a,b) for k in ['fee_usdt','spread_depth_cost_usdt','slippage_cost_usdt'])/capital
    net=gross-cost-information
    if not math.isclose(net,(a['cash_delta_usdt']+b['cash_delta_usdt'])/capital-information,abs_tol=1e-12):
        raise ValueError('PNL_RECONCILIATION')
    return dict(status='RESOLVED',metrics=dict(gross_return=gross,trading_cost=cost,information_cost=information),
                net_return=net,executed=True,exit_reason=exit['reason'],raw_realized_pnl_usdt=exit['realized_pnl_usdt'])

def resolve_case(store,config,case):
    if store.get('alpha_outcome',case['id'],True):return store.get('alpha_outcome',case['id'])
    if instant(utc())<instant(case['due_at']):return dict(status='PENDING',case_id=case['id'])
    snapshot=store.get('alpha_snapshot',case['snapshot_id']);maintain_exits(store,config,case,snapshot)
    native={a:metrics_for(store,config,case,a,'NATIVE') for a in config['arms']}
    matched={a:metrics_for(store,config,case,a,'MATCHED') for a in config['arms']}
    if any(v['status']=='OPEN_OR_MISSING_EXIT' for v in list(native.values())+list(matched.values())):
        return dict(status='WAITING_EXIT',case_id=case['id'])
    initial=store.get('flow_window',case['flow_window_id'])
    final=capture_flow(store,config,case['id']+':final')
    economic={s:{side:economic_label(initial,final,s,side,utc(),case['due_at'],config['flow_ratio_threshold'])
                 for side in ['BUY','SELL']} for s in config['universe']}
    outcome=store.append('alpha_outcome',case['id'],dict(id=case['id'],case_id=case['id'],received_at=utc(),
             due_at=case['due_at'],native=native,matched=matched,economic=economic,final_flow_window_id=final['id'],
             mode='FORWARD_PAPER',information_cost_status=config['information_cost_status']))
    from .audit import audit_runtime
    audit=audit_runtime(store.root)
    store.append('event_memory',case['id'],dict(id=case['id'],due_at=case['due_at'],received_at=utc(),
         origin='FORWARD_PAPER',independent_audit_passed=audit['passed'],case_hash=digest(case),outcome_hash=digest(outcome),
         state=snapshot['state'],flow=initial['events'],native=native,economic=economic,
         decisions=[store.get('alpha_decision',case['id']+':'+a,True) for a in config['arms']],
         failure_causes=['UNPROVEN_FORCED_FLOW_SPILLOVER']+[a+':'+v['status'] for a,v in native.items() if v['metrics'] is None]))
    return outcome

def perform_task(store,loop,protocol,task,action):
    key=task['task_key'];done=store.get('alpha_worker_receipt',key,True)
    if done:
        loop.ack(key,task['claim_token'],result=done['result'],actual_cash_units=0,actual_time_seconds=done['elapsed_seconds'],succeeded=done['success'])
        return done
    started=time.monotonic();success=True
    try:
        if action['operation']=='NEW_CASE':result=new_case(store,loop,protocol,action['case_id'])
        elif action['operation']=='RESOLVE':result=resolve_case(store,protocol['config'],store.get('alpha_case',action['case_id']))
        elif action['operation']=='GROWTH_FEASIBILITY':result=growth_feasibility(store)
        else:raise ValueError('UNKNOWN_WORKER_OPERATION')
    except Exception as exc:
        success=False;result=dict(status='FAILED',error=type(exc).__name__+':'+str(exc))
        store.failure('alpha_worker',exc,task_key=key)
    elapsed=math.ceil(time.monotonic()-started)
    receipt=store.append('alpha_worker_receipt',key,dict(id=key,operation=action['operation'],input_hash=task['task']['input_hash'],
             result=result,output_hash=digest(result),elapsed_seconds=elapsed,success=success,
             actual_incremental_cash_charge=0,charge_status='PUBLIC_DATA_EXISTING_SUBSCRIPTION_NO_NEW_COMMITMENT',at=utc()))
    loop.ack(key,task['claim_token'],result=result,actual_cash_units=0,actual_time_seconds=elapsed,succeeded=success)
    return receipt

def growth_feasibility(store):
    from one_trading.autonomy.growth import capture_source
    url='https://www.circle.com/blog/circle-match-get-a-rebate-to-mint-set-your-price-to-redeem'
    try:
        source,text=capture_source(store,url)
        result=dict(status='DATA_BLOCKED',candidate='GROWTH_DEMAND_SUPPLY_CHANGE',source=source,
          observable='Issuer claims incentives for matched institutional USDC mint/redemption',
          cheapest_falsifier='Can matched mint volume and attributable incremental Aave borrowing demand be measured in aligned units?',
          missing=['matched mint volume and coverage','attributable incremental Aave borrow demand','token demand/capture conversion'],
          inference='Mint incentives do not by themselves establish AAVE holder buying demand.',
          expected_decision_delay='30-day economic / 90-day investment research seed',
          decision='RETAIN_OBSERVATION_QUEUE_NO_FORWARD_GROWTH_CLAIM',text_hash=source['text_hash'])
    except Exception as exc:result=dict(status='DATA_BLOCKED',candidate='GROWTH_DEMAND_SUPPLY_CHANGE',url=url,error=str(exc))
    return store.append('alpha_growth_feasibility','initial',result)

def enqueue(loop,store,eid,key,kind,action,due=None):
    old=loop.db.execute('SELECT task_key FROM tasks WHERE task_key=?',(key,)).fetchone()
    if old:return
    h=digest(action);loop.record_data(eid,h)
    store.append('alpha_task_action',key,dict(id=key,**action))
    loop.enqueue_task(key,eid,kind,input_hash=h,cash_units=0,time_seconds=600,
        decision_value_score=1,due_at=due,slot=key if kind=='due_label' else '')

def tick(root):
    root=Path(root).resolve()
    with process_lock(root):
        store=Store(root);loop=None
        try:
            protocol=verify_authority(store);config=protocol['config'];loop=open_loop(root,config)
            # Integrity is first. Preserve all claims; reconcile durable worker receipts.
            from .audit import audit_runtime
            audit_runtime(root)
            for t in loop.db.execute("SELECT task_key FROM tasks WHERE state='CLAIMED'").fetchall():
                task=loop.get_task(t['task_key']);action=store.get('alpha_task_action',t['task_key'])
                perform_task(store,loop,protocol,task,action)
            cases=store.all('alpha_case')
            for case in cases:
                if not store.get('alpha_outcome',case['id'],True):
                    advance(store,config,case)
                    if instant(utc())>=instant(case['due_at']):
                        enqueue(loop,store,claim_id(store,'semantic'),'resolve:'+case['id']+':'+utc()[:16],
                                'due_label',dict(operation='RESOLVE',case_id=case['id'],attempt_minute=utc()[:16]),case['due_at'])
            bootstrap='alpha-bootstrap-'+protocol.get('cohort_id','v1')
            if not cases and not loop.db.execute("SELECT 1 FROM experiments WHERE id=?",(bootstrap,)).fetchone():
                loop.register_experiment(ExperimentSpec(bootstrap,bootstrap,'CASHFLOW',1,protocol['registered_at'],
                        (instant(protocol['new_case_deadline'])+timedelta(days=2)).isoformat(),0,config['research_time_budget_seconds'],'FORWARD_PAPER'))
                loop.record_data(bootstrap,protocol['config_hash'])
            unfinished=[c for c in cases if not store.get('alpha_outcome',c['id'],True)]
            if not unfinished and instant(utc())<instant(protocol['new_case_deadline']):
                prefix='batch-'+str(len(cases)+1).zfill(5)+'-'+instant(utc()).strftime('%Y%m%d%H')
                # A failed pre-case collection gets a new actual attempt, never a fabricated older cutoff.
                eid=claim_id(store,'semantic') if cases else bootstrap
                attempts=loop.db.execute("SELECT task_key,state FROM tasks WHERE task_key LIKE ?",('new:'+prefix+'%',)).fetchall()
                if not any(t['state'] in ('QUEUED','CLAIMED') for t in attempts) and len(attempts)<config['max_model_attempts']:
                    key=prefix+'-a'+str(len(attempts)+1)
                    enqueue(loop,store,eid,'new:'+key,'research',dict(operation='NEW_CASE',case_id=key))
            if not loop.db.execute("SELECT 1 FROM experiments WHERE id='growth-feasibility'").fetchone():
                loop.register_experiment(ExperimentSpec('growth-feasibility','GROWTH_DEMAND_SUPPLY_CHANGE','CAPITAL_GROWTH',1,
                    protocol['registered_at'],protocol['new_case_deadline'],0,7200,'FORWARD_PAPER'))
                loop.record_data('growth-feasibility',protocol['config_hash'])
                enqueue(loop,store,'growth-feasibility','growth-initial','cheap_falsifier',dict(operation='GROWTH_FEASIBILITY'))
            receipts=[]
            for _ in range(3):
                task=loop.tick()
                if not task:break
                receipts.append(perform_task(store,loop,protocol,task,store.get('alpha_task_action',task['task_key'])))
            evidence=submit(store,loop)
            result=dict(at=utc(),status='FORWARD_RUNNING',worker_receipts=[r['id'] for r in receipts],
                 registered_batches=len(store.all('alpha_case')),matured_batches=len(store.all('alpha_outcome')),
                 evidence=evidence,next_task='RESOLVE_DUE_THEN_NEXT_NONOVERLAPPING_BATCH',priority='HEURISTIC_PRIORITY',
                 edge_proven=False,live_trading_enabled=False)
            store.append('alpha_tick',uuid.uuid4().hex,result)
            return result
        finally:
            if loop:loop.close()
            store.close()
