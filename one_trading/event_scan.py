"""Preregistered all-hour exploration and result-dependent bounded follow-ups.

Used receipts are historical reconstruction, never information received at their
past signal clock. Fixed capital sleeves and overlapping signals are not samples.
"""
import copy
from datetime import timedelta
from .autonomy.store import utc,instant,digest,Store,process_lock
from .hypothesis_loop import ABSORPTION,DELAY,PANEL,RULES,read_inputs,select,parent_record,state
H=3600000
PROTOCOL=dict(name='EVENT_SCAN_V1',clock_rule='ALL_ELIGIBLE_COMPLETED_HOURS_ASCENDING',
    comparison='ALL_ARMS_SAME_HOURLY_CLOCKS_INDEPENDENT_SINGLE_POSITION_SLEEVES',
    overlap='EARLIEST_SIGNAL_BLOCKS_UNTIL_EXIT_INCLUDING_ENTRY_DELAY',
    cluster='TRANSITIVE_OVERLAP_OF_26H_SIGNAL_TO_EXIT_WINDOWS_ACROSS_BOTH_MECHANISMS',
    features='ONLY_COMPLETED_BARS_BEFORE_SIGNAL',labels='EXACT_2H_DELAY_24H_HOLD_OPEN_NO_IMPUTATION',
    execution='HOURLY_OPEN_RECONSTRUCTION_NOT_EXECUTABLE_QUOTES',reuse=True,independent_evidence=False)
ACTIONS={'EVALUATE_EVENTS','COVERAGE_DIAGNOSTIC','FAILURE_ATTRIBUTION','FREEZE_FORWARD_CANDIDATE','ACQUIRE_MISSING','REPAIR_EXECUTION','EXPIRY_REVIEW'}

def register(s,parent_id,action='EVALUATE_EVENTS',mechanism=None,ready_at=None):
    from .economic_batches import implementation_hash
    parent=parent_record(s,parent_id);oldspec=s.get('hypothesis_spec',parent_id,True)
    mechanism=mechanism or parent.get('mechanism_id')
    if mechanism not in RULES or (action=='EVALUATE_EVENTS' and mechanism==PANEL):raise ValueError('EVENT_MECHANISM_REQUIRED')
    if action not in ACTIONS:raise ValueError('UNREGISTERED_ACTION')
    key='event-'+digest([parent_id,digest(parent),mechanism,action,PROTOCOL])[:24]
    old=s.get('hypothesis_spec',key,True)
    if old:return old
    if oldspec is None:raise ValueError('EXECUTED_TYPED_PARENT_REQUIRED')
    c=s.all('batch_config')[-1];now=utc()
    changes={
      'EVALUATE_EVENTS':('Replace sparse 48h sampling with every eligible hourly clock; retain causal gates.', 'Sparse sampling, rather than economic failure, may explain the old zero-entry result.', 'No eligible event or nonpositive cost-adjusted result fails this exploration.'),
      'COVERAGE_DIAGNOSTIC':('Measure coverage and signal frequency; do not change thresholds.', 'No new economic assumption; diagnose absent events before gathering more data.', 'Complete raw coverage with zero qualifying signals refutes occurrence in this archive.'),
      'FAILURE_ATTRIBUTION':('Attribute failure to raw price movement, explicit costs and adverse excursion.', 'No new economic assumption; distinguish absent gross edge from cost erosion before proposing a variant.', 'Nonpositive gross PnL refutes a cost-only explanation; negative net cannot promote the strategy.'),
      'FREEZE_FORWARD_CANDIDATE':('Freeze the unchanged specification as a candidate for a separately registered forward cohort.', 'Historical positive PnL is provisional and may be noise or shared market exposure.', 'Cost-adjusted forward performance must beat cash and the price comparator under a new sealed protocol; no alpha is allocated here.'),
      'ACQUIRE_MISSING':('List only the observed missing fields/intervals for bounded acquisition.', 'Missing data cannot be inferred as an economic rejection.', 'Without exactly the missing observations no financial evaluation is admissible.'),
      'REPAIR_EXECUTION':('Isolate the recorded execution error and its recovery requirements.', 'An execution error provides no evidence against the economic mechanism.', 'The original immutable request must complete without changed inputs or duplicate charges.'),
      'EXPIRY_REVIEW':('Record expiry and require a newly registered version before further execution.', 'An expired specification cannot be relabelled as a financial rejection.', 'No result obtained after the frozen expiry can be attributed to the expired request.')}
    change,assumption,falsifier=changes[action]
    content={k:copy.deepcopy(v) for k,v in oldspec.items() if k not in ('id','spec_hash','stored_at')}
    content.update(parent_trial_id=parent_id,parent_result_hash=digest(parent),mechanism_id=mechanism,
        selection_rule=dict(id=mechanism,parameters=RULES[mechanism]),sampling_protocol=PROTOCOL,action=action,
        comparison_arms=['PRICE_ONLY','ECONOMIC_RULE','AI_UNAVAILABLE','CASH'],change_reason=change,
        learned=dict(parent_status=parent['status'],parent_aggregates=parent.get('aggregates'),no_adoption=True),
        observed_facts=dict(status=parent['status'],counts=parent.get('counts',{}),aggregates=parent.get('aggregates'),error=parent.get('error')),
        changed_assumption=assumption,retained_conditions=dict(rule=RULES[mechanism],entry_delay_seconds=7200,horizon_seconds=86400,costs=oldspec['costs']),
        falsification=falsifier,raw_requirements=dict(receipts=parent['source_receipts'],new_requests=0,missing=parent.get('missing',[parent['error']] if parent.get('error') else [])),
        source_receipts=parent['source_receipts'],origin='HISTORICAL_EXPLORATION_REUSED_NOT_INDEPENDENT',
        lineage_class='SAMPLING_PROTOCOL_REVISION' if action=='EVALUATE_EVENTS' else 'RESULT_DEPENDENT_DIAGNOSTIC',
        registered_at=now,source_version=implementation_hash(),version=2,ready_at=ready_at or now,
        expiry=(instant(now)+timedelta(days=3)).isoformat(),resource_limits=dict(trial_seconds=c['max_trial_seconds'],model_calls=0,cash_units=0,http_requests=0),
        ai=dict(status='NOT_REQUESTED',reason='BLOCKED_MODEL_CALL_CAP_ZERO; proposal/counterargument not run; rule-based next action only.',model=None,prompt_version=None,schema_hash=None))
    content['input_binding']=dict(source_hash=digest(parent['source_receipts']),available_at=max(r['received_at'] for rs in parent['source_receipts'].values() for r in rs),cutoff=now,missing_policy='DATA_INSUFFICIENT_NO_IMPUTATION',known_used_parent_archive=True)
    spec=s.append('hypothesis_spec',key,dict(id=key,spec_hash=digest(content),**content))
    s.append('next_hypothesis',key,dict(id=key,spec_id=key,spec_hash=spec['spec_hash'],parent_trial=parent_id,change=change,due_at=spec['ready_at'],priority=0 if action=='EVALUATE_EVENTS' else 2,selection_reason=change,queue_schema=3))
    state(s,key,'REGISTERED',spec_hash=spec['spec_hash']);return spec


def successor(s,parent):
    if parent.get('action') and parent['action']!='EVALUATE_EVENTS':return None
    actions={'NO_ELIGIBLE_EVENTS':'COVERAGE_DIAGNOSTIC','NEGATIVE_AFTER_COST':'FAILURE_ATTRIBUTION',
        'POSITIVE_EXPLORATORY':'FREEZE_FORWARD_CANDIDATE','DATA_INSUFFICIENT':'ACQUIRE_MISSING','EXECUTION_ERROR':'REPAIR_EXECUTION','EXPIRED':'EXPIRY_REVIEW'}
    action=actions.get(parent['status'])
    if action is None:raise ValueError('RESULT_HAS_NO_FOLLOWUP_CONTRACT')
    c=s.all('batch_config')[-1]
    return register(s,parent['id'],action,ready_at=(instant(parent.get('completed_at',utc()))+timedelta(hours=c['batch_interval_hours'])).isoformat())


def contexts(data):
    series={s:{r[0]:r for r in d['bars']} for s,d in data.items()};common=set.intersection(*(set(b) for b in series.values()))
    out=[];excluded=[]
    for t in sorted(common):
        assets={};missing=[]
        for symbol,b in series.items():
            need=list(range(t-25*H,t,H))+list(range(t+2*H,t+27*H,H))
            gaps=[x for x in need if x not in b]
            if gaps:missing.append(dict(symbol=symbol,field='hourly_bars',open_ms=gaps));continue
            fs=sorted([f for f in data[symbol]['funding'] if f['fundingTime']<=t],key=lambda f:f['fundingTime'])
            if len(fs)<3 or t-fs[-1]['fundingTime']>12*H:missing.append(dict(symbol=symbol,field='funding_coverage',signal_ms=t));continue
            last=b[t-H];q=float(last[7]);mean=sum(float(b[x][7]) for x in range(t-25*H,t-H,H))/24
            if min(q,mean)<=0:missing.append(dict(symbol=symbol,field='positive_quote_volume',signal_ms=t));continue
            assets[symbol]=dict(past_24h_return=float(last[4])/float(b[t-24*H][1])-1,aggressive_sell_share=1-float(last[10])/q,
                volume_ratio=q/mean,hour_return=float(last[4])/float(last[1])-1,last_feature_close_ms=last[6],quote_volume=q,taker_buy_quote=float(last[10]),
                latest_funding=float(fs[-1]['fundingRate']),last_three_funding_sum=sum(float(f['fundingRate']) for f in fs[-3:]),funding_source_time=fs[-1]['fundingTime'])
        if missing:excluded.append(dict(signal_ms=t,reason='MISSING_REQUIRED_FEATURE_OR_LABEL_COVERAGE',missing=missing));continue
        out.append(dict(id=str(t),signal_ms=t,entry_ms=t+2*H,exit_ms=t+26*H,assets=assets))
    return out,excluded


def funnel(cs,mechanism):
    gates=[]
    for c in cs:
        a=c['assets']
        if mechanism==ABSORPTION:
            gates.extend([[v['past_24h_return']<0,v['aggressive_sell_share']>=.6,v['volume_ratio']>=1.5,abs(v['hour_return'])<=.002] for v in a.values()])
        else:
            b=a['BTCUSDT'];gates.append([b['past_24h_return']>=.02,b['volume_ratio']>=1.5,any(a[s]['past_24h_return']-b['past_24h_return']<=-.01 for s in ('ETHUSDT','SOLUSDT'))])
    names=['negative_24h','sell_share_ge_0.6','volume_ge_1.5','abs_impact_le_0.002'] if mechanism==ABSORPTION else ['BTC_24h_ge_0.02','BTC_volume_ge_1.5','alt_residual_le_minus_0.01']
    return dict(unit='ASSET_CLOCK' if mechanism==ABSORPTION else 'CLOCK',total=len(gates),stages=[dict(gate=n,individual_pass=sum(g[i] for g in gates),cumulative_pass=sum(all(g[:i+1]) for g in gates),cumulative_fail=sum(not all(g[:i+1]) for g in gates)) for i,n in enumerate(names)])


def clusters(cs,lineage):
    # Group both mechanisms using only signal clocks, never realized returns.
    events=[c['signal_ms'] for c in cs if any(select(c,m) for m in (ABSORPTION,DELAY))];result={};last_end=-1;start=None
    for t in events:
        if t>=last_end:start=t
        result[t]='shared-'+digest([lineage,start])[:16];last_end=t+26*H
    return result


def evaluate(s,spec):
    if spec['sampling_protocol']!=PROTOCOL or spec['action'] not in ACTIONS:raise ValueError('UNREGISTERED_EVENT_PROTOCOL')
    if spec['action']!='EVALUATE_EVENTS':return diagnose(s,spec)
    data=read_inputs(s,spec);cs,excluded=contexts(data);prices={s:{b[0]:b for b in d['bars']} for s,d in data.items()}
    lineage=spec['input_binding']['source_hash'];groups=clusters(cs,lineage);rows=[];busy={a:-1 for a in spec['comparison_arms']};signals=[];overlaps=[]
    for c in cs:
        ranked=sorted(c['assets'],key=lambda s:(c['assets'][s]['past_24h_return'],s));economic=select(c,spec['mechanism_id'])
        if economic:signals.append(dict(signal_ms=c['signal_ms'],symbol=economic,cluster_id=groups[c['signal_ms']]))
        choices={'PRICE_ONLY':ranked[0] if c['assets'][ranked[0]]['past_24h_return']<0 else None,'ECONOMIC_RULE':economic,'CASH':None,'AI_UNAVAILABLE':None}
        for arm,symbol in choices.items():
            if arm=='AI_UNAVAILABLE':rows.append(dict(case_id=c['id'],arm=arm,status='NOT_RUN',net_usdt=None,information_cost_usdt=0));continue
            candidate=symbol;blocked=bool(symbol and c['signal_ms']<busy[arm]);reason='OVERLAP_UNTIL_PRIOR_EXIT' if blocked else 'QUALIFYING_SIGNAL' if symbol else 'FILTER_NO_SIGNAL'
            if blocked:
                overlaps.append(dict(arm=arm,signal_ms=c['signal_ms'],symbol=symbol,blocked_until_ms=busy[arm],reason=reason));symbol=None
            entry=exit=None;qty=gross=fee=slip=spread=mae=0.
            if symbol:
                entry=float(prices[symbol][c['entry_ms']][1]);exit=float(prices[symbol][c['exit_ms']][1]);qty=1250/(entry*1.0016)
                gross=qty*(exit-entry);turnover=qty*(entry+exit);fee=turnover*.001;slip=turnover*.0005;spread=turnover*.0001
                mae=min(0,min(float(prices[symbol][t][3])/entry-1 for t in range(c['entry_ms'],c['exit_ms'],H)))
                busy[arm]=c['exit_ms']
            rows.append(dict(case_id=c['id'],arm=arm,status='HISTORICAL_RECONSTRUCTION',symbol=symbol,candidate_symbol=candidate,action='LONG' if symbol else 'NO_TRADE',reason=reason,
                signal_ms=c['signal_ms'],entry_ms=c['entry_ms'],exit_ms=c['exit_ms'],actual_received_at=spec['input_binding']['available_at'],
                feature_available_by_ms=c['signal_ms']-1,entry_price=entry,exit_price=exit,quantity=qty,gross_usdt=gross,
                fee_usdt=fee,slippage_usdt=slip,spread_usdt=spread,trading_cost_usdt=fee+slip+spread,information_cost_usdt=0,funding_usdt=0,net_usdt=gross-fee-slip-spread,
                max_adverse_return=mae,not_a_fill=True,cluster_id=groups.get(c['signal_ms']),input_lineage=lineage))
    aggregates={a:dict(units=0 if a=='AI_UNAVAILABLE' else len(cs),trades=sum(r['arm']==a and bool(r.get('symbol')) for r in rows),
        gross_usdt=None if a=='AI_UNAVAILABLE' else sum(r['gross_usdt'] for r in rows if r['arm']==a),
        trading_cost_usdt=None if a=='AI_UNAVAILABLE' else sum(r['trading_cost_usdt'] for r in rows if r['arm']==a),
        net_usdt=None if a=='AI_UNAVAILABLE' else sum(r['net_usdt'] for r in rows if r['arm']==a)) for a in spec['comparison_arms']}
    eco=aggregates['ECONOMIC_RULE'];status='NO_ELIGIBLE_EVENTS' if not signals else 'POSITIVE_EXPLORATORY' if eco['net_usdt']>0 else 'NEGATIVE_AFTER_COST'
    return dict(status=status,action=spec['action'],sampling_protocol=PROTOCOL,mechanism_id=spec['mechanism_id'],spec_id=spec['id'],spec_hash=spec['spec_hash'],
        source_receipts=spec['source_receipts'],input_hash=digest(data),contexts=cs,coverage_exclusions=excluded,filter_funnel=funnel(cs,spec['mechanism_id']),eligible_events=signals,overlap_exclusions=overlaps,rows=rows,aggregates=aggregates,
        comparison_reason='All arms inspect identical hourly clocks with independent single-position sleeves; archived prices/cost assumptions are not executable historical quotes.',
        failure_attribution=dict(gross_usdt=eco['gross_usdt'],cost_usdt=eco['trading_cost_usdt'],net_usdt=eco['net_usdt'],
            primary='NO_OCCURRENCE' if not signals else 'NEGATIVE_RAW_RETURN' if eco['gross_usdt']<=0 else 'COST_EROSION' if eco['net_usdt']<=0 else 'UNVALIDATED_POSITIVE',
            eligible_signals=len(signals),accepted_entries=eco['trades'],worst_adverse_return=min([r['max_adverse_return'] for r in rows if r['arm']=='ECONOMIC_RULE' and r['symbol']] or [0])),
        counts=dict(historical_hypothesis_trials=1,historical_windows=len(cs),historical_hypothetical_entries=sum(a['trades'] for a in aggregates.values()),independent_forward_events=0,paper_fills=0,matured_forward_results=0,data_collection_runs=0,model_calls=0),
        historical_contamination=True,independent_validation=False,adopted=False)


def diagnose(s,spec):
    parent=parent_record(s,spec['parent_trial_id']);action=spec['action']
    facts={'COVERAGE_DIAGNOSTIC':dict(eligible_events=len(parent.get('eligible_events',[])),filter_funnel=parent.get('filter_funnel'),coverage_exclusions=parent.get('coverage_exclusions')),
      'FAILURE_ATTRIBUTION':parent.get('failure_attribution'),'FREEZE_FORWARD_CANDIDATE':dict(candidate_spec_id=parent['id'],source_spec_hash=parent.get('spec_hash'),new_forward_registration_required=True),
      'ACQUIRE_MISSING':dict(missing=spec['raw_requirements']['missing'],http_calls=0,acquisition_status='BLOCKED_NO_SPECIFIED_PUBLIC_REPAIR_ADAPTER'),
      'REPAIR_EXECUTION':dict(error=parent.get('error'),recovery_status='NEEDS_CODE_REPAIR_NOT_ECONOMIC_REJECTION'),
      'EXPIRY_REVIEW':dict(expired_spec_id=parent['id'],new_version_required=True)}[action]
    return dict(status='FORWARD_CANDIDATE_FROZEN' if action=='FREEZE_FORWARD_CANDIDATE' else 'DIAGNOSTIC_COMPLETED',action=action,spec_id=spec['id'],spec_hash=spec['spec_hash'],mechanism_id=spec['mechanism_id'],
        source_receipts=spec['source_receipts'],facts=facts,counts={k:0 for k in ('historical_hypothesis_trials','historical_windows','historical_hypothetical_entries','independent_forward_events','paper_fills','matured_forward_results','data_collection_runs','model_calls')},
        adopted=False,independent_validation=False,next_action='WAIT_NEW_EVIDENCE_OR_REVIEW_REGISTERED_FORWARD_PROTOCOL',ai_status='BLOCKED_MODEL_CALL_CAP_ZERO')


def seed(root):
    """Freeze both sampling revisions before reading any outcome prices."""
    with process_lock(root):
        s=Store(root)
        try:
            result=[]
            for m in (ABSORPTION,DELAY):
                parents=[r for r in s.all('hypothesis_trial') if r['mechanism_id']==m and not r.get('sampling_protocol')]
                if not parents:raise ValueError('REAL_COMPLETED_GRID_PARENT_REQUIRED')
                result.append(register(s,parents[0]['id'],mechanism=m))
            return dict(status='REGISTERED_BEFORE_OUTCOME_COMPUTATION',specs=result)
        finally:s.close()


def rebind_pending(s):
    """Explicit operational successor to a pending old-code queue item.

    Preserve the old specification and its due/expiry/budgets. Only the executor
    revision changes; the immutable replacement explains the compatibility fix.
    """
    from .economic_batches import implementation_hash
    migrated=[];version=implementation_hash()
    for old in s.all('hypothesis_spec'):
        if s.get('hypothesis_trial',old['id'],True) or old['source_version']==version or s.get('hypothesis_queue_revision',old['id'],True):continue
        content={k:copy.deepcopy(v) for k,v in old.items() if k not in ('id','spec_hash','stored_at')}
        key='revision-'+digest([old['id'],version])[:24]
        content.update(source_version=version,registered_at=utc(),version=old['version']+1,
            operational_predecessor=old['id'],operational_reason='Maintenance/audit fixes; economic conditions, inputs, deadline and budgets unchanged.')
        new=s.append('hypothesis_spec',key,dict(id=key,spec_hash=digest(content),**content))
        oldq=s.get('next_hypothesis',old['id']);q={k:copy.deepcopy(v) for k,v in oldq.items() if k!='stored_at'}
        q.update(id=key,spec_id=key,spec_hash=new['spec_hash'])
        s.append('next_hypothesis',key,q)
        s.append('hypothesis_queue_revision',old['id'],dict(id=old['id'],replacement_id=key,reason=content['operational_reason'],at=utc()))
        state(s,key,'REGISTERED',spec_hash=new['spec_hash']);migrated.append(key)
    return migrated
