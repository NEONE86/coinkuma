"""Independent arithmetic/chronology verifier; does not call the study simulator.

This verifies frozen recorded outcomes and schedule completeness, not profitability.
Source identity is additionally established by the existing Bybit raw audit.
"""
import argparse
import bisect
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path


def verify(result, frames, p, source_times=None):
    times=[datetime.fromisoformat(f['received_at']).timestamp() for f in frames]
    fee=p['fee_per_side'];slip=p['slippage_per_side'];delay=p['decision_latency_seconds']+p['execution_delay_seconds']
    checked=0;cache={}
    def near(a,b):
        if not math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-9):raise ValueError('ARITHMETIC_MISMATCH')
    split=times[0]+(times[-1]-times[0])*p['split_fraction']
    grid=[];target=times[0]+p['warmup_seconds']
    while target<=times[-1]:
        grid.append(bisect.bisect_left(times,target));target+=p['decision_stride_seconds']
    allowed=set(grid)
    def check(r,partition=None):
        nonlocal checked
        i=r['decision_index'];s=r['symbol'];entry=r['entry_index'];exit=r['exit_index']
        if i not in allowed:raise ValueError('DECISION_GRID')
        partition=partition or r.get('partition')
        bound=bisect.bisect_left(times,split) if partition=='DISCOVERY' else len(times)
        end=bisect.bisect_left(times,times[i]+r['horizon']);expected_entry=bisect.bisect_left(times,times[i]+delay)
        if 'partition' in r:
            expected_partition='DISCOVERY' if times[i]+r['horizon']+delay<split else 'VALIDATION' if times[i]>=split else 'PURGED_CROSS_SPLIT'
            if r['partition']!=expected_partition:raise ValueError('PARTITION_LABEL')
        entry_status=None
        if end>=bound or expected_entry>=bound or end<=expected_entry:entry_status='RIGHT_CENSORED'
        else:
            quote=frames[expected_entry]['quotes'][s]
            age=times[expected_entry]-datetime.fromisoformat(quote['exchange_at']).timestamp()
            spread=(quote['ask']-quote['bid'])/((quote['ask']+quote['bid'])/2)*10000
            notional=min(p['position_fraction'],p['max_position_fraction'],p['max_total_exposure_fraction'])*r['start_equity']
            if not 0<=age<=p['max_quote_age_seconds'] or spread>p['max_spread_bps'] or (source_times is not None and source_times[expected_entry][s]<=times[i]+p['decision_latency_seconds']):entry_status='ENTRY_QUOTE_REJECTED'
            elif notional<p['min_notional_usdt']:entry_status='MIN_NOTIONAL'
            elif notional/(quote['ask']*(1+slip))>quote['ask_size']*p['max_book_participation']:entry_status='ENTRY_DEPTH_REJECTED'
        if entry_status is not None:
            if r['status']!=entry_status or entry is not None or exit is not None:raise ValueError('ENTRY_STATUS')
            return
        if entry!=expected_entry or exit is None:raise ValueError('ENTRY_CHRONOLOGY')
        if exit>=bound:raise ValueError('CROSS_SPLIT_LEAK')
        q=frames[entry]['quotes'][s];qty=r['quantity'];price=q['ask']*(1+slip)
        near(r['entry_price'],price)
        near(qty*price,min(p['position_fraction'],p['max_position_fraction'],p['max_total_exposure_fraction'])*r['start_equity'])
        if qty>q['ask_size']*p['max_book_participation']:raise ValueError('ENTRY_DEPTH')
        def executable(j,decision):
            quote=frames[j]['quotes'][s];age=times[j]-datetime.fromisoformat(quote['exchange_at']).timestamp()
            spread=(quote['ask']-quote['bid'])/((quote['ask']+quote['bid'])/2)*10000
            if not 0<=age<=p['max_quote_age_seconds'] or spread>p['max_spread_bps']:raise ValueError('QUOTE_INELIGIBLE')
            if source_times is not None and source_times[j][s]<=times[decision]+p['decision_latency_seconds']:raise ValueError('PRE_DECISION_RAW_RECEIPT')
        executable(entry,i)
        spend=qty*price*(1+fee);sell=frames[exit]['quotes'][s]['bid']*(1-slip)
        near(r['entry_spend'],spend);near(r['exit_price'],sell)
        near(r['pnl'],qty*sell*(1-fee)-spend)
        near(r['net_return'],r['pnl']/spend)
        near(r['fees'],qty*(price+sell)*fee)
        near(r['slippage'],qty*(q['ask']+frames[exit]['quotes'][s]['bid'])*slip)
        near(r['log_growth'],math.log1p(r['pnl']/r['start_equity']))
        if r['status']=='CLOSED':
            if exit!=bisect.bisect_left(times,times[r['exit_trigger_index']]+delay):raise ValueError('EXIT_CHRONOLOGY')
            if qty>frames[exit]['quotes'][s]['bid_size']*p['max_book_participation']:raise ValueError('EXIT_DEPTH')
            if qty*sell<p['min_notional_usdt']:raise ValueError('EXIT_MIN_NOTIONAL')
            executable(exit,r['exit_trigger_index'])
        elif r['mark_or_fill']!='FINAL_BID_MARK_NOT_FILL':raise ValueError('FALSE_FILL')
        # Rebuild volatility from pre-decision midpoints, independently of feature builder.
        start=bisect.bisect_right(times,times[i]-300)-1
        mids=[(f['quotes'][s]['bid']+f['quotes'][s]['ask'])/2 for f in frames[start:i+1]]
        vol=math.sqrt(sum(math.log(b/a)**2 for a,b in zip(mids,mids[1:])))
        current=frames[i]['quotes'][s];spread=(current['ask']-current['bid'])/((current['ask']+current['bid'])/2)
        near(r['risk'],max(p['stop_vol_floor_fraction'],vol)+2*(fee+slip)+spread)
        key=(i,s,r['horizon'],r['rr'])
        if key not in cache:
            end=bisect.bisect_left(times,times[i]+r['horizon']);values=[];touch='NEITHER';first=None;gap=False
            for j in range(entry,end+1):
                if j>entry and times[j]-times[j-1]>p['max_observation_gap_seconds']:gap=True
                quote=frames[j]['quotes'][s]
                age=times[j]-datetime.fromisoformat(quote['exchange_at']).timestamp()
                if not 0<=age<=p['max_quote_age_seconds']:gap=True;continue
                value=quote['bid']*(1-slip)*(1-fee)/(price*(1+fee))-1
                values.append(value)
                if touch=='NEITHER':
                    if value>=r['rr']*r['risk']:touch='TARGET';first=j
                    elif value<=-r['risk']:touch='STOP';first=j
            cache[key]=(max(values),min(values),touch,first if first is not None else end,gap)
        mfe,mae,touch,first,gap=cache[key];near(r['mfe_net'],mfe);near(r['mae_net'],mae)
        if r['touch']!=touch:raise ValueError('TOUCH_ORDER')
        end=bisect.bisect_left(times,times[i]+r['horizon'])
        trigger=first;expected_exit=bisect.bisect_left(times,times[trigger]+delay)
        def can_exit(j,k):
            if j>=bound:return False
            try:executable(j,k)
            except ValueError:return False
            quote=frames[j]['quotes'][s]
            return qty<=quote['bid_size']*p['max_book_participation'] and qty*quote['bid']*(1-slip)>=p['min_notional_usdt']
        rejected=None
        if not can_exit(expected_exit,trigger):
            rejected='EXIT_DEPTH_OR_QUOTE_REJECTED';trigger=end;expected_exit=bisect.bisect_left(times,times[end]+delay)
        status='CLOSED' if can_exit(expected_exit,trigger) else 'OPEN_UNLIQUIDATED'
        if status!='CLOSED':expected_exit=bound-1
        if (r['exit_trigger_index'],exit,r['status'],r['exit_rejection'])!=(trigger,expected_exit,status,rejected):raise ValueError('EXIT_POLICY')
        trade_gap=any(times[j]-times[j-1]>p['max_observation_gap_seconds'] for j in range(entry+1,exit+1))
        if (r['gap_uncertain'],r['trade_gap_uncertain'])!=(gap,trade_gap):raise ValueError('GAP_EVIDENCE')
        checked+=1
    for r in result['opportunities']:check(r)
    expected_opportunities=set()
    for i in grid:
        start=bisect.bisect_right(times,times[i]-300)-1
        if start<0 or times[i]-300-times[start]>p['max_observation_gap_seconds']:continue
        if any(times[j]-times[j-1]>p['max_observation_gap_seconds'] for j in range(start+1,i+1)):continue
        for symbol in p['symbols']:
            if any(times[j]-datetime.fromisoformat(frames[j]['quotes'][symbol]['exchange_at']).timestamp()>p['max_quote_age_seconds'] for j in range(start,i+1)):continue
            expected_opportunities.update((i,symbol,h,rr) for h in p['horizons_seconds'] for rr in p['net_reward_risk'])
    actual_opportunities=[(r['decision_index'],r['symbol'],r['horizon'],r['rr']) for r in result['opportunities']]
    if len(actual_opportunities)!=len(set(actual_opportunities)) or set(actual_opportunities)!=expected_opportunities:raise ValueError('OPPORTUNITY_COVERAGE')
    signal_cache={}
    def selected_asset(index,name):
        key=(index,name)
        if key in signal_cache:return signal_cache[key]
        candidates=[]
        for symbol in frames[index]['quotes']:
            anchors={n:bisect.bisect_right(times,times[index]-n)-1 for n in (30,60,300)}
            start=anchors[300]
            if start<0 or any(times[index]-n-times[k]>p['max_observation_gap_seconds'] for n,k in anchors.items()):continue
            if any(times[j]-times[j-1]>p['max_observation_gap_seconds'] for j in range(start+1,index+1)):continue
            if any(times[j]-datetime.fromisoformat(frames[j]['quotes'][symbol]['exchange_at']).timestamp()>p['max_quote_age_seconds'] for j in range(start,index+1)):continue
            mids=[(frames[j]['quotes'][symbol]['bid']+frames[j]['quotes'][symbol]['ask'])/2 for j in range(start,index+1)]
            returns={n:mids[-1]/mids[k-start]-1 for n,k in anchors.items()}
            q=frames[index]['quotes'][symbol];cost=2*(fee+slip)+(q['ask']-q['bid'])/mids[-1]
            vol=math.sqrt(sum(math.log(b/a)**2 for a,b in zip(mids,mids[1:])))
            trend=returns[300]>cost and returns[60]>0
            position=(mids[-1]-min(mids))/(max(mids)-min(mids)) if max(mids)>min(mids) else None
            eligible=(name=='TREND' and trend) or (name=='IMBALANCE_ASSIST' and trend and (q['bid_size']-q['ask_size'])/(q['bid_size']+q['ask_size'])>=.2) or (name=='REVERSION' and position is not None and position<=.2 and returns[30]>0 and vol>=cost/2)
            if eligible:candidates.append((abs(returns[300]),symbol))
        signal_cache[key]=max(candidates)[1] if candidates else None
        return signal_cache[key]
    for strategy in result['strategies']:
        equity=p['capital_usdt'];last=-1;peak=equity;dd=0;lossrun=0;maxloss=0
        if strategy['partition'] not in ('DISCOVERY','VALIDATION'):raise ValueError('STRATEGY_PARTITION')
        lo,hi=(times[0],split) if strategy['partition']=='DISCOVERY' else (split,times[-1])
        for r in strategy['trades']:
            decision=times[r['decision_index']]
            if decision<lo or decision+r['horizon']+delay>=hi:raise ValueError('STRATEGY_TIME')
            if (r['horizon'],r['rr'])!=(strategy['horizon'],strategy['rr']):raise ValueError('STRATEGY_SPEC')
            if selected_asset(r['decision_index'],strategy['strategy'])!=r['symbol']:raise ValueError('STRATEGY_SIGNAL')
            if dd>=p['max_drawdown_fraction']:raise ValueError('DRAWDOWN_HALT')
            if r['decision_index']<=last:raise ValueError('OVERLAPPING_POSITIONS')
            near(r['start_equity'],equity);check(r,strategy['partition'])
            if r.get('exit_index') is None:continue
            if strategy['partition']=='DISCOVERY' and times[r['exit_index']]>=times[0]+(times[-1]-times[0])*p['split_fraction']:raise ValueError('CROSS_SPLIT_LEAK')
            trade_dd=0
            for j in range(r['entry_index'],r['exit_index']+1):
                mark=equity-r['entry_spend']+r['quantity']*frames[j]['quotes'][r['symbol']]['bid']*(1-slip)*(1-fee)
                peak=max(peak,mark);dd=max(dd,1-mark/peak);trade_dd=max(trade_dd,1-mark/peak)
            near(r['intratrade_drawdown'],trade_dd);near(r['peak_equity'],peak)
            equity+=r['pnl'];last=r['exit_index']
            lossrun=lossrun+1 if r['pnl']<0 else 0;maxloss=max(maxloss,lossrun)
        near(strategy['equity'],equity);near(strategy['pnl'],equity-p['capital_usdt'])
        near(strategy['log_growth'],math.log(equity/p['capital_usdt']));near(strategy['max_drawdown'],dd)
        validate_summary(strategy['summary'],strategy['trades'],strategy['rr'])
        clean=[r for r in strategy['trades'] if r['status']=='CLOSED' and not (r['gap_uncertain'] or r.get('trade_gap_uncertain',False))]
        uncertain=any(r.get('gap_uncertain') or r.get('trade_gap_uncertain') or r['status']=='OPEN_UNLIQUIDATED' for r in strategy['trades'])
        expected='INSUFFICIENT'
        if strategy['partition']=='VALIDATION' and not uncertain:
            if len(clean)>=p['candidate_min_validation_trades'] and equity>p['capital_usdt']:expected='CANDIDATE'
            elif len(clean)>=p['reject_min_validation_trades'] and equity<=p['capital_usdt']:expected='REJECT'
        if strategy['verdict']!=expected:raise ValueError('PROMOTION_VERDICT')
        mean=math.log(equity/p['capital_usdt'])/len(clean) if clean and len(clean)==sum('pnl' in r for r in strategy['trades']) else None
        if strategy['mean_log_growth']!=mean or strategy['max_consecutive_losses']!=maxloss:raise ValueError('GROWTH_OR_LOSS_RUN')
        cursor=0;busy=-1;halted=False
        for i in grid:
            if halted or i<=busy or times[i]<lo or times[i]+strategy['horizon']+delay>=hi:continue
            symbol=selected_asset(i,strategy['strategy'])
            if symbol is None:continue
            if cursor>=len(strategy['trades']):raise ValueError('MISSING_STRATEGY_DECISION')
            row=strategy['trades'][cursor];cursor+=1
            if (row['decision_index'],row['symbol'])!=(i,symbol):raise ValueError('STRATEGY_SCHEDULE')
            if row['entry_index'] is not None:
                busy=row['exit_index'];halted=row['status']!='CLOSED' or row['intratrade_drawdown']>=p['max_drawdown_fraction']
        if cursor!=len(strategy['trades']):raise ValueError('EXTRA_STRATEGY_DECISION')
    keys=[(s['strategy'],s['horizon'],s['rr'],s['partition']) for s in result['strategies']]
    expected_keys={(s,h,rr,part) for s in p['strategies'] for h in p['horizons_seconds'] for rr in p['net_reward_risk'] for part in ('DISCOVERY','VALIDATION')}
    if len(keys)!=len(set(keys)) or set(keys)!=expected_keys:raise ValueError('STRATEGY_COVERAGE')
    group_keys=[(g['symbol'],g['horizon'],g['rr']) for g in result['opportunity_groups']]
    if len(group_keys)!=len(set(group_keys)) or set(group_keys)!={(s,h,rr) for s in p['symbols'] for h in p['horizons_seconds'] for rr in p['net_reward_risk']}:raise ValueError('GROUP_COVERAGE')
    for group in result['opportunity_groups']:
        records=[r for r in result['opportunities'] if (r['symbol'],r['horizon'],r['rr'])==(group['symbol'],group['horizon'],group['rr'])]
        validate_summary({k:v for k,v in group.items() if k not in ('symbol','horizon','rr','independent_time_blocks_upper_bound')},records,group['rr'])
        if group['independent_time_blocks_upper_bound']!=int((times[-1]-times[0])/group['horizon']):raise ValueError('INDEPENDENCE_COUNT')
    return dict(status='PASS_RECORDED_OUTCOMES',checked_positions=checked,scope='Independent causal risk, MFE/MAE, touch order, delays, depth, fees, arithmetic, overlap, partition and drawdown. Raw source audit required separately; not alpha proof.')


def validate_summary(actual,records,rr):
    """Recompute report fields independently; no producer summary imports."""
    closed=[r for r in records if r['status']=='CLOSED']
    clean=[r for r in closed if not (r['gap_uncertain'] or r.get('trade_gap_uncertain',False))]
    count=len(clean);touches={k:sum(r['touch']==k for r in clean) for k in ('TARGET','STOP','NEITHER')}
    binary=touches['TARGET']+touches['STOP']
    def quantile(key,q):
        values=sorted(r[key] for r in clean)
        return values[round((len(values)-1)*q)] if values else None
    expected=dict(attempts=len(records),closed=len(closed),clean_observed=count,
        right_censored=sum(r['status']=='RIGHT_CENSORED' for r in records),
        entry_depth_rejected=sum(r['status']=='ENTRY_DEPTH_REJECTED' for r in records),
        exit_rejected=sum(bool(r.get('exit_rejection')) for r in records),
        uncertain_or_open=sum(r['status']=='OPEN_UNLIQUIDATED' or r.get('gap_uncertain',False) or r.get('trade_gap_uncertain',False) for r in records),
        touches=touches,binary_break_even=1/(rr+1),target_first_rate=touches['TARGET']/binary if binary else None,
        actual_positive_return_rate=sum(r['net_return']>0 for r in clean)/count if count else None,
        mean_net_return=sum(r['net_return'] for r in clean)/count if count else None,
        mean_log_growth=sum(r['log_growth'] for r in clean)/count if count else None,
        mfe_net_quantiles={str(q):quantile('mfe_net',q) for q in (.5,.9,.99)},
        mae_net_quantiles={str(q):quantile('mae_net',q) for q in (.5,.1,.01)},
        hindsight_positive_mfe=sum(r['mfe_net']>0 for r in clean),sample_warning='OVERLAPPING_OUTCOMES_NOT_INDEPENDENT')
    if actual!=expected:raise ValueError('SUMMARY_MISMATCH')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--study',type=Path,required=True);parser.add_argument('--source',type=Path,required=True);a=parser.parse_args()
    result=json.loads((a.study/'result.json').read_bytes());p=json.loads((a.study/'frozen-protocol.json').read_bytes())
    expected=json.loads((Path(__file__).resolve().parents[2]/'configs/opportunity_v1.json').read_bytes())
    if p!=expected:raise ValueError('FROZEN_PROTOCOL_MISMATCH')
    frames=[json.loads(l) for l in (a.source/'frames.jsonl').read_bytes().splitlines()]
    events=[json.loads(l) for l in (a.source/'events.jsonl').read_bytes().splitlines()]
    provenance={x['frame_id']:x['source_seqs'] for x in events if x['kind']=='frame_provenance'}
    needed={seq for row in provenance.values() for seq in row.values()};receipts={}
    with (a.source/'raw.jsonl').open('rb') as stream:
        for line in stream:
            row=json.loads(line)
            if row['seq'] in needed:receipts[row['seq']]=datetime.fromisoformat(row['received_at']).timestamp()
    sources=[{s:receipts[n] for s,n in provenance[f['id']].items()} for f in frames]
    report=verify(result,frames,p,sources)
    report['result_sha256']=hashlib.sha256((a.study/'result.json').read_bytes()).hexdigest()
    (a.study/'independent-audit.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))

if __name__=='__main__':main()
