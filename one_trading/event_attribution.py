"""Versioned raw-path diagnostics beside immutable EVENT_SCAN_V1 results.

No new data, models, fills or significance allocations. Registered future designs
are proposals awaiting received observations, not automatically enabled policies.
"""
import argparse,json,math,time
from datetime import timedelta
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,process_lock
from .hypothesis_loop import read_inputs,select,ABSORPTION,DELAY
from .alpha_loop.worker import open_loop
from .alpha_loop.reference.research_loop import ExperimentSpec
from .batch_control import claim_owned,ack_preserving_overrun

H=3600000
HORIZONS=(1,2,4,8,12,24)
ARMS=('ECONOMIC_SELECTION','BTC','PRICE_SELECTOR','CASH')
PROTOCOL=dict(name='MATCHED_EVENT_ATTRIBUTION_V1',horizons=list(HORIZONS),
    comparison='SAME_ACCEPTED_EVENT_ENTRY_CLOCK_AND_NOMINAL_EXPOSURE_NO_REENTRY',
    scope='TRAINING_OR_EXPLORATORY',causal_identification=False,beta_adjusted_alpha=False,
    new_http_requests=0,model_calls=0,paper_fills=0,new_forward_events=0)

def path(bars,symbol,entry_ms,costs):
    notional=costs['capital_usdt']*costs['exposure_fraction']
    b=bars.get(symbol) if symbol else None
    rate=(costs['fee_bps_per_side']+costs['slippage_bps_per_side']+costs['assumed_half_spread_bps'])/10000
    en=float(b[entry_ms][1]) if b else None
    qty=notional/(en*(1+rate)) if b else 0.
    out=[]
    for h in HORIZONS:
        ex=float(b[entry_ms+h*H][1]) if b else None
        gross=qty*(ex-en) if b else 0.;turnover=qty*(en+ex) if b else 0.
        fee=turnover*costs['fee_bps_per_side']/10000;slip=turnover*costs['slippage_bps_per_side']/10000;spread=turnover*costs['assumed_half_spread_bps']/10000
        out.append(dict(hours=h,exit_ms=entry_ms+h*H,exit_price=ex,price_return=ex/en-1 if b else 0.,gross_usdt=gross,
            fee_usdt=fee,slippage_usdt=slip,spread_usdt=spread,trading_cost_usdt=fee+slip+spread,
            net_usdt=gross-fee-slip-spread,information_cost_usdt=0,funding_usdt=0,
            mae=min([0.]+[float(b[t][3])/en-1 for t in range(entry_ms,entry_ms+h*H,H)]) if b else 0.,
            mfe=max([0.]+[float(b[t][2])/en-1 for t in range(entry_ms,entry_ms+h*H,H)]) if b else 0.))
    return dict(symbol=symbol,action='LONG' if b else 'NO_TRADE',nominal_opportunity_usdt=notional,
        cash_deployed_usdt=notional if b else 0.,entry_price=en,quantity=qty,
        preentry_2h_return=en/float(b[entry_ms-2*H][1])-1 if b else None,markouts=out)

def calculate(s,parents):
    events=[];specs={r['id']:r for r in s.all('hypothesis_spec')}
    for parent in parents:
        spec=specs[parent['id']];data=read_inputs(s,spec);bars={k:{r[0]:r for r in v['bars']} for k,v in data.items()}
        contexts={c['id']:c for c in parent['contexts']}
        for row in parent['rows']:
            if row['arm']!='ECONOMIC_RULE' or not row.get('symbol'):continue
            context=contexts[row['case_id']]
            rank=min(context['assets'],key=lambda x:(context['assets'][x]['past_24h_return'],x))
            symbols={'ECONOMIC_SELECTION':row['symbol'],'BTC':'BTCUSDT','PRICE_SELECTOR':rank if context['assets'][rank]['past_24h_return']<0 else None,'CASH':None}
            arms={a:path(bars,symbols[a],row['entry_ms'],spec['costs']) for a in ARMS}
            event=dict(id=digest([parent['id'],row['case_id']])[:24],parent_trial_id=parent['id'],parent_result_hash=digest(parent),
                mechanism_id=parent['mechanism_id'],cluster_id=row['cluster_id'],signal_ms=row['signal_ms'],entry_ms=row['entry_ms'],
                source_receipts=parent['source_receipts'],actually_received_at=spec['input_binding']['available_at'],
                historical_feature_time_ms=row['signal_ms']-1,signal_features=context['assets'],arms=arms,
                selected_minus_BTC_net_usdt=[arms['ECONOMIC_SELECTION']['markouts'][i]['net_usdt']-arms['BTC']['markouts'][i]['net_usdt'] for i in range(len(HORIZONS))])
            assert math.isclose(arms['ECONOMIC_SELECTION']['markouts'][-1]['net_usdt'],row['net_usdt'],abs_tol=1e-9)
            events.append(event)
    groups={}
    for e in events:
        for key in (e['mechanism_id'],e['cluster_id']):
            g=groups.setdefault(key,dict(events=0,arms={a:{str(h):dict(gross_usdt=0.,cost_usdt=0.,net_usdt=0.) for h in HORIZONS} for a in ARMS}))
            g['events']+=1
            for a in ARMS:
                for m in e['arms'][a]['markouts']:
                    z=g['arms'][a][str(m['hours'])];z['gross_usdt']+=m['gross_usdt'];z['cost_usdt']+=m['trading_cost_usdt'];z['net_usdt']+=m['net_usdt']
    return dict(protocol=PROTOCOL,events=events,groups=groups,
        original_full_policy_baselines={p['id']:p['aggregates']['PRICE_ONLY'] for p in parents},
        baseline_unique_trades=24,baseline_duplicate_uses=24,
        counts=dict(accepted_economic_events=len(events),temporal_clusters=len({e['cluster_id'] for e in events}),
            reused_raw_files=len({r['sha256'] for p in parents for rs in p['source_receipts'].values() for r in rs}),
            new_data_collections=0,model_attempts=0,synthetic_market_results=0,paper_fills=0,new_forward_events=0,new_mature_forward_results=0),
        limitations=['Five archived clusters are not proof of independent samples.',
            'BTC path is a market-direction comparator, not a beta-adjusted causal decomposition.',
            'All horizons are post-hoc diagnostics; 4h was suggested by this used archive.',
            'Hourly opens and assumed costs are not executable historical quotes.'])

def next_designs(result,registered_at,parent_hash):
    start=(instant(registered_at).replace(minute=0,second=0,microsecond=0)+timedelta(hours=1))
    common=dict(version=1,registered_at=registered_at,parent_artifact_hash=parent_hash,origin='POST_HOC_GENERATED',
        scope='UNUSED_FUTURE_DATA_ONLY_NOT_ENABLED_FOR_TRADING',status='WAITING_NEW_EVIDENCE',
        universe=['BTCUSDT','ETHUSDT','SOLUSDT'],signal_rules='EXACT_PARENT_EVENT_SCAN_V1_GATES_UNCHANGED',
        entry_delay_seconds=7200,horizons_seconds=[14400,86400],exposure_fraction=.125,capital_usdt=10000,
        costs=dict(fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1,information_usdt=0,spot_funding=0),
        start_signal_at=start.isoformat(),first_feature_receipt_due=(start+timedelta(seconds=120)).isoformat(),
        earliest_full_label_due=(start+timedelta(hours=26,seconds=120)).isoformat(),end_signal_at=(start+timedelta(days=14)).isoformat(),
        final_label_due=(start+timedelta(days=14,hours=26,seconds=120)).isoformat(),
        cluster='TRANSITIVE_26H_ACROSS_BOTH_MECHANISMS_EARLIEST_ACCEPTED_EVENT_NO_EXTRA_4H_REENTRY',
        controls=['UNCHANGED_24H','BTC_MATCHED_CLOCK','PRICE_SELECTOR_MATCHED_CLOCK','CASH'],
        information_timing='Receive frozen feature receipts before decision; late downloaded history is OUT_OF_SAMPLE_HISTORICAL, never prospective.',
        required_fields=['closed_hour_OHLCV_and_taker_quote','last_three_funding_with_timestamps','actual_received_at','raw_sha256','fixed_signal_and_entry_clocks','entry_and_horizon_quotes_or_explicit_missing','same_event_cluster_id'],
        minimum_clusters=10,termination='14-day fixed signal window; fewer than 10 complete clusters is INCONCLUSIVE. No threshold relaxation or new alpha allocation.',
        model_calls=0,http_budget='EXISTING_LIMITS_ONLY_NO_NEW_COLLECTOR_ENABLED',auto_execution=False,
        resume_condition='A separately registered bounded collection request must preserve actual receipt clocks and unchanged authority caps before accepting a new event.')
    one=dict(common,id='temporal-decay-'+parent_hash[:16],explanation='Short relative recovery dissipates during the remaining 20h.',
        observed_fact='All five old +4h price moves were positive; costs left one +4h loser and four +24h losers.',
        inference='A shorter holding horizon may retain an otherwise transient effect.',counter_explanation='Horizon cherry-picking and one BTC outlier can explain the pattern.',
        primary_metric='Equal-cluster mean of selected net 4h minus selected net 24h, same original entries.',
        reject_if='With at least 10 complete clusters: primary mean <= 0 OR selected 4h mean net <= CASH OR matched PRICE_SELECTOR. Otherwise only further validation, no proven edge.')
    two=dict(common,id='selection-vs-market-'+parent_hash[:16],explanation='Absorption selects weak assets; delay losses chiefly track the common market.',
        observed_fact='Both losing ETH absorption paths lagged BTC; delay relative paths were mixed while both absolute paths lost.',
        inference='Selection weakness and common exposure have different fingerprints, beyond a universal timing explanation.',counter_explanation='Five clusters and BTC as an imperfect market proxy can create this separation by chance.',
        primary_metric='For each mechanism, equal-cluster mean selected minus BTC net at 24h; report 4h only as fixed secondary diagnostic.',
        reject_if='With at least 10 complete clusters per mechanism: absorption relative mean >= 0 rejects persistent weak selection; delay relative mean < 0 rejects market-only explanation. Insufficient mechanism counts remain INCONCLUSIVE.')
    return [one,two]

def run(root):
    from .economic_batches import implementation_hash,readonly_rows
    with process_lock(root):
        s=Store(root);loop=None
        try:
            c=s.all('batch_config')[-1]
            if c['source_hash']!=implementation_hash():raise ValueError('SOURCE_VERSION_NOT_REGISTERED')
            parents=[r for r in s.all('hypothesis_trial') if r.get('action')=='EVALUATE_EVENTS']
            if len(parents)!=2:raise ValueError('TWO_ORIGINAL_EVENT_PARENTS_REQUIRED')
            key='attribution-'+digest([PROTOCOL,[digest(p) for p in parents]])[:24]
            old=s.get('event_attribution_result',key,True)
            spec=s.get('event_attribution_spec',key,True)
            if not spec:spec=s.append('event_attribution_spec',key,dict(id=key,registered_at=utc(),protocol=PROTOCOL,source_version=implementation_hash(),
                parents=[dict(id=p['id'],hash=digest(p)) for p in parents],deadline=(instant(utc())+timedelta(days=1)).isoformat(),max_seconds=240))
            alpha=Path(c['alpha_runtime']);p=readonly_rows(alpha,'alpha_protocol')[0]
            with process_lock(alpha):
                loop=open_loop(alpha,p['config'])
                exists=loop.db.execute('SELECT 1 FROM tasks WHERE task_key=?',(key,)).fetchone()
                task=loop.get_task(key) if exists else None
                if not task:
                    loop.register_experiment(ExperimentSpec(key,'offline-event-attribution/'+key,'CAPITAL_GROWTH',1,spec['registered_at'],spec['deadline'],0,240,'HISTORICAL'))
                    loop.record_data(key,digest(spec));loop.enqueue_task(key,key,'research',input_hash=digest(spec),cash_units=0,time_seconds=240)
                    task=loop.get_task(key)
                interrupted=task['state']=='CLAIMED' and not old
                if task['state']=='QUEUED':task=claim_owned(loop,[key])
                if not task:return dict(status='BUDGET_BLOCKED',id=key)
                if not old:
                    begin=time.monotonic();value=calculate(s,parents);seconds=max(240 if interrupted else 0,math.ceil(time.monotonic()-begin))
                    old=s.append('event_attribution_result',key,dict(id=key,completed_at=utc(),spec_hash=digest(spec),elapsed_seconds=seconds,**value))
                accounting=ack_preserving_overrun(loop,task,old,old['elapsed_seconds'])
                if accounting['status']!='ACKNOWLEDGED':return accounting
                for design in next_designs(old,old['completed_at'],digest(old)):
                    if not s.get('event_followup_design',design['id'],True):s.append('event_followup_design',design['id'],design)
                return dict(status='ATTRIBUTION_COMPLETED',id=key,result_hash=digest(old),counts=old['counts'],accounting=accounting,new_alpha_allocations=0)
        finally:
            if loop:loop.close()
            s.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);a=p.parse_args();print(json.dumps(run(a.runtime),indent=2))
