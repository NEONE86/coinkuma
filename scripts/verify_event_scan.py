"""Independent EVENT_SCAN_V1 arithmetic; stdlib only, no production imports."""
import json,hashlib,math
from pathlib import Path
from datetime import datetime
H=3600000
ABS='SPOT_SELL_PRESSURE_ABSORPTION';DELAY='CROSS_ASSET_INFORMATION_DELAY'
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def eq(a,b):
    if isinstance(b,dict):
        assert isinstance(a,dict) and set(a)==set(b),(set(a),set(b))
        for k in b:eq(a[k],b[k])
    elif isinstance(b,list):
        assert isinstance(a,list) and len(a)==len(b),(len(a),len(b))
        for x,y in zip(a,b):eq(x,y)
    elif type(b) in (int,float):assert type(a) in (int,float) and math.isfinite(a) and math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8),(a,b)
    else:assert type(a)==type(b) and a==b,(a,b)
def choose(v,m):
    if m==ABS:yes=[s for s,x in v.items() if x['past_24h_return']<0 and x['aggressive_sell_share']>=.6 and x['volume_ratio']>=1.5 and abs(x['hour_return'])<=.002];key=lambda s:(v[s]['past_24h_return'],s)
    else:
        b=v['BTCUSDT'];yes=[s for s in ('ETHUSDT','SOLUSDT') if b['past_24h_return']>=.02 and b['volume_ratio']>=1.5 and v[s]['past_24h_return']-b['past_24h_return']<=-.01];key=lambda s:(v[s]['past_24h_return']-b['past_24h_return'],s)
    return min(yes,key=key) if yes else None

def verify_event(root,spec,trial):
    assert spec['sampling_protocol']==dict(name='EVENT_SCAN_V1',clock_rule='ALL_ELIGIBLE_COMPLETED_HOURS_ASCENDING',comparison='ALL_ARMS_SAME_HOURLY_CLOCKS_INDEPENDENT_SINGLE_POSITION_SLEEVES',overlap='EARLIEST_SIGNAL_BLOCKS_UNTIL_EXIT_INCLUDING_ENTRY_DELAY',cluster='TRANSITIVE_OVERLAP_OF_26H_SIGNAL_TO_EXIT_WINDOWS_ACROSS_BOTH_MECHANISMS',features='ONLY_COMPLETED_BARS_BEFORE_SIGNAL',labels='EXACT_2H_DELAY_24H_HOLD_OPEN_NO_IMPUTATION',execution='HOURLY_OPEN_RECONSTRUCTION_NOT_EXECUTABLE_QUOTES',reuse=True,independent_evidence=False)
    assert spec['source_receipts']==trial['source_receipts'] and spec['action']==trial['action']=='EVALUATE_EVENTS'
    assert spec['entry_delay_seconds']==7200 and spec['horizon_seconds']==86400
    assert spec['costs']==dict(capital_usdt=10000,exposure_fraction=.125,fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1,information_cost_per_call_usdt=.1,information_cost_status='SENSITIVITY_NOT_INVOICE',spot_funding_cashflow=0,accounting='FIXED_NOTIONAL_NOT_COMPOUNDED')
    data={};decoded={}
    for s,rs in spec['source_receipts'].items():
        values=[]
        for r in rs:
            raw=(Path(root)/'raw/batch'/(r['sha256']+'.bin')).read_bytes();assert hashlib.sha256(raw).hexdigest()==r['sha256'];values.append(json.loads(raw))
            assert datetime.fromisoformat(r['received_at'].replace('Z','+00:00'))<=datetime.fromisoformat(spec['registered_at'].replace('Z','+00:00'))
        bars,funding=values;assert len({b[0] for b in bars})==len(bars)
        assert all(b[6]==b[0]+H-1 and b[6]<datetime.fromisoformat(rs[0]['received_at'].replace('Z','+00:00')).timestamp()*1000 for b in bars)
        decoded[s]=dict(bars=bars,funding=funding);data[s]=dict(bars={b[0]:b for b in bars},funding=funding)
    assert set(data)=={'BTCUSDT','ETHUSDT','SOLUSDT'} and digest(decoded)==trial['input_hash']
    cs=[];excluded=[];common=set.intersection(*(set(x['bars']) for x in data.values()))
    for t in sorted(common):
        vals={};missing=[]
        for s,d in data.items():
            b=d['bars'];gaps=[x for x in list(range(t-25*H,t,H))+list(range(t+2*H,t+27*H,H)) if x not in b]
            if gaps:missing.append(dict(symbol=s,field='hourly_bars',open_ms=gaps));continue
            f=sorted((r for r in d['funding'] if r['fundingTime']<=t),key=lambda r:r['fundingTime'])
            if len(f)<3 or t-f[-1]['fundingTime']>12*H:missing.append(dict(symbol=s,field='funding_coverage',signal_ms=t));continue
            last=b[t-H];q=float(last[7]);mean=sum(float(b[x][7]) for x in range(t-25*H,t-H,H))/24
            if min(q,mean)<=0:missing.append(dict(symbol=s,field='positive_quote_volume',signal_ms=t));continue
            assert 0<=float(last[10])<=q
            vals[s]=dict(past_24h_return=float(last[4])/float(b[t-24*H][1])-1,aggressive_sell_share=1-float(last[10])/q,volume_ratio=q/mean,hour_return=float(last[4])/float(last[1])-1,last_feature_close_ms=last[6],quote_volume=q,taker_buy_quote=float(last[10]),latest_funding=float(f[-1]['fundingRate']),last_three_funding_sum=sum(float(r['fundingRate']) for r in f[-3:]),funding_source_time=f[-1]['fundingTime'])
        if missing:excluded.append(dict(signal_ms=t,reason='MISSING_REQUIRED_FEATURE_OR_LABEL_COVERAGE',missing=missing))
        else:cs.append(dict(id=str(t),signal_ms=t,entry_ms=t+2*H,exit_ms=t+26*H,assets=vals))
    eq(trial['contexts'],cs);eq(trial['coverage_exclusions'],excluded)
    lineage=spec['input_binding']['source_hash'];groups={};last=-1;start=None
    for c in cs:
        t=c['signal_ms']
        if any(choose(c['assets'],m) for m in (ABS,DELAY)):
            if t>=last:start=t
            groups[t]='shared-'+digest([lineage,start])[:16];last=t+26*H
    arms=['PRICE_ONLY','ECONOMIC_RULE','AI_UNAVAILABLE','CASH'];assert spec['comparison_arms']==arms
    rows={(r['case_id'],r['arm']):r for r in trial['rows']};assert len(rows)==len(trial['rows'])==len(cs)*4
    assert set(rows)=={(c['id'],a) for c in cs for a in arms}
    agg={a:dict(units=0 if a=='AI_UNAVAILABLE' else len(cs),trades=0,gross_usdt=0.,trading_cost_usdt=0.,net_usdt=0.) for a in arms};busy=dict.fromkeys(arms,-1);signals=[];overlaps=[];worst=0.;gates=[]
    for c in cs:
        t=c['signal_ms'];v=c['assets'];m=spec['mechanism_id'];economic=choose(v,m)
        if economic:signals.append(dict(signal_ms=t,symbol=economic,cluster_id=groups[t]))
        if m==ABS:gates.extend([[x['past_24h_return']<0,x['aggressive_sell_share']>=.6,x['volume_ratio']>=1.5,abs(x['hour_return'])<=.002] for x in v.values()])
        else:gates.append([v['BTCUSDT']['past_24h_return']>=.02,v['BTCUSDT']['volume_ratio']>=1.5,any(v[s]['past_24h_return']-v['BTCUSDT']['past_24h_return']<=-.01 for s in ('ETHUSDT','SOLUSDT'))])
        rank=min(v,key=lambda s:(v[s]['past_24h_return'],s));choices=dict(PRICE_ONLY=rank if v[rank]['past_24h_return']<0 else None,ECONOMIC_RULE=economic,CASH=None)
        eq(rows[c['id'],'AI_UNAVAILABLE'],dict(case_id=c['id'],arm='AI_UNAVAILABLE',status='NOT_RUN',net_usdt=None,information_cost_usdt=0))
        for arm,sym in choices.items():
            candidate=sym;blocked=bool(sym and t<busy[arm]);reason='OVERLAP_UNTIL_PRIOR_EXIT' if blocked else 'QUALIFYING_SIGNAL' if sym else 'FILTER_NO_SIGNAL'
            if blocked:overlaps.append(dict(arm=arm,signal_ms=t,symbol=sym,blocked_until_ms=busy[arm],reason=reason));sym=None
            qty=gross=fee=slip=spread=mae=0.;entry=exit=None
            if sym:
                b=data[sym]['bars'];entry=float(b[t+2*H][1]);exit=float(b[t+26*H][1]);qty=1250/(entry*1.0016);gross=qty*(exit-entry);turn=qty*(entry+exit)
                fee=turn*.001;slip=turn*.0005;spread=turn*.0001;mae=min(0,min(float(b[x][3])/entry-1 for x in range(t+2*H,t+26*H,H)));busy[arm]=t+26*H;agg[arm]['trades']+=1
                if arm=='ECONOMIC_RULE':worst=min(worst,mae)
            expected=dict(case_id=c['id'],arm=arm,status='HISTORICAL_RECONSTRUCTION',symbol=sym,candidate_symbol=candidate,action='LONG' if sym else 'NO_TRADE',reason=reason,signal_ms=t,entry_ms=t+2*H,exit_ms=t+26*H,actual_received_at=spec['input_binding']['available_at'],feature_available_by_ms=t-1,entry_price=entry,exit_price=exit,quantity=qty,gross_usdt=gross,fee_usdt=fee,slippage_usdt=slip,spread_usdt=spread,trading_cost_usdt=fee+slip+spread,information_cost_usdt=0,funding_usdt=0,net_usdt=gross-fee-slip-spread,max_adverse_return=mae,not_a_fill=True,cluster_id=groups.get(t),input_lineage=lineage)
            eq(rows[c['id'],arm],expected)
            for k in ('gross_usdt','trading_cost_usdt','net_usdt'):agg[arm][k]+=expected[k]
    for k in ('gross_usdt','trading_cost_usdt','net_usdt'):agg['AI_UNAVAILABLE'][k]=None
    eq(trial['aggregates'],agg);eq(trial['eligible_events'],signals);eq(trial['overlap_exclusions'],overlaps)
    names=['negative_24h','sell_share_ge_0.6','volume_ge_1.5','abs_impact_le_0.002'] if spec['mechanism_id']==ABS else ['BTC_24h_ge_0.02','BTC_volume_ge_1.5','alt_residual_le_minus_0.01']
    eq(trial['filter_funnel'],dict(unit='ASSET_CLOCK' if spec['mechanism_id']==ABS else 'CLOCK',total=len(gates),stages=[dict(gate=n,individual_pass=sum(g[i] for g in gates),cumulative_pass=sum(all(g[:i+1]) for g in gates),cumulative_fail=sum(not all(g[:i+1]) for g in gates)) for i,n in enumerate(names)]))
    e=agg['ECONOMIC_RULE'];assert trial['status']==('NO_ELIGIBLE_EVENTS' if not signals else 'POSITIVE_EXPLORATORY' if e['net_usdt']>0 else 'NEGATIVE_AFTER_COST')
    eq(trial['failure_attribution'],dict(gross_usdt=e['gross_usdt'],cost_usdt=e['trading_cost_usdt'],net_usdt=e['net_usdt'],primary='NO_OCCURRENCE' if not signals else 'NEGATIVE_RAW_RETURN' if e['gross_usdt']<=0 else 'COST_EROSION' if e['net_usdt']<=0 else 'UNVALIDATED_POSITIVE',eligible_signals=len(signals),accepted_entries=e['trades'],worst_adverse_return=worst))
    eq(trial['counts'],dict(historical_hypothesis_trials=1,historical_windows=len(cs),historical_hypothetical_entries=sum(x['trades'] for x in agg.values()),independent_forward_events=0,paper_fills=0,matured_forward_results=0,data_collection_runs=0,model_calls=0))
    assert not trial['adopted'] and not trial['independent_validation'] and trial['historical_contamination']
    return {a:agg[a]['net_usdt'] for a in arms}
