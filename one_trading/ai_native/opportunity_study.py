"""Frozen, offline-only spot-long opportunity distribution and sequential controls."""
import argparse
import bisect
import hashlib
import json
import math
import time
from pathlib import Path
from .causal_features import build
from .engine import Protocol, canonical, instant, sha256


def load_tape(source):
    frames=[json.loads(l) for l in (source/"frames.jsonl").read_bytes().splitlines()]
    provenance={r["frame_id"]:r["source_seqs"] for r in (json.loads(l) for l in (source/"events.jsonl").read_bytes().splitlines()) if r["kind"]=="frame_provenance"}
    needed={n for r in provenance.values() for n in r.values()};receipts={}
    with (source/"raw.jsonl").open('rb') as stream:
        for line in stream:
            r=json.loads(line)
            if r['seq'] in needed:receipts[r['seq']]=instant(r['received_at']).timestamp()
    times=[instant(f['received_at']).timestamp() for f in frames]
    assert all(b>a for a,b in zip(times,times[1:]))
    return dict(exchange_times=[{s:instant(q['exchange_at']).timestamp() for s,q in f['quotes'].items()} for f in frames],frames=frames,times=times,source_times=[{s:receipts[n] for s,n in provenance[f['id']].items()} for f in frames])


def feature_grid(tape,p):
    frames=tape['frames'];times=tape['times'];start=times[0];grid=[];left=0
    target=start+p['warmup_seconds']
    while target<=times[-1]:
        i=bisect.bisect_left(times,target)
        if i>=len(times):break
        while left+1<=i and times[left+1]<=times[i]-300:left+=1
        f=build(frames[left:i+1],frames[i]['received_at'],vars(Protocol()))
        # Bounded-window evidence identity is deliberately not the full-prefix hash.
        f['window_sha256']=f.pop('prefix_sha256');f['available_seconds']=times[i]-start
        grid.append(dict(index=i,features=f))
        target+=p['decision_stride_seconds']
    return grid


def signal(row,strategy):
    w=row['windows']
    if any(w[str(n)]['status']!='AVAILABLE' for n in (30,60,300)):return False
    trend=w['300']['return_fraction']>row['round_trip_cost_bps']/10000 and w['60']['return_fraction']>0
    if strategy=='TREND':return trend
    if strategy=='IMBALANCE_ASSIST':return trend and row['imbalance']>=.2
    if strategy=='REVERSION':return w['300']['price_position'] is not None and w['300']['price_position']<=.2 and w['30']['return_fraction']>0 and w['300']['realized_vol']>=row['round_trip_cost_bps']/20000
    return False


def simulate(tape,p,i,symbol,horizon,rr,risk,equity=10000., prior_peak=None):
    ts=tape['times'];fs=tape['frames'];src=tape['source_times'];fee=p['fee_per_side'];slip=p['slippage_per_side']
    delay=p['decision_latency_seconds']+p['execution_delay_seconds'];end_time=ts[i]+horizon
    entry=bisect.bisect_left(ts,ts[i]+delay);end=bisect.bisect_left(ts,end_time)
    row=dict(decision_index=i,symbol=symbol,horizon=horizon,rr=rr,risk=risk,start_equity=equity,entry_index=None,exit_index=None,status='RIGHT_CENSORED',touch=None)
    if end>=len(ts) or entry>=len(ts) or end<=entry:return row
    def quote_ok(j,decision):
        q=fs[j]['quotes'][symbol];age=ts[j]-(tape['exchange_times'][j][symbol] if 'exchange_times' in tape else instant(q['exchange_at']).timestamp());spread=(q['ask']-q['bid'])/((q['ask']+q['bid'])/2)*10000
        return src[j][symbol]>decision and 0<=age<=p['max_quote_age_seconds'] and spread<=p['max_spread_bps']
    if not quote_ok(entry,ts[i]+p['decision_latency_seconds']):row['status']='ENTRY_QUOTE_REJECTED';return row
    q=fs[entry]['quotes'][symbol];price=q['ask']*(1+slip)
    notional=min(p['position_fraction'],p['max_position_fraction'],p['max_total_exposure_fraction'])*equity
    qty=notional/price;spend=notional*(1+fee)
    if notional<p['min_notional_usdt']:row['status']='MIN_NOTIONAL';return row
    if qty>q['ask_size']*p['max_book_participation']:row['status']='ENTRY_DEPTH_REJECTED';return row
    row.update(entry_index=entry,entry_price=price,quantity=qty,entry_notional=notional,entry_spend=spend)
    # Outcome-only scan, never used by signal or asset selection.
    path=[];touch=None;trigger=None;gap=False
    for j in range(entry,end+1):
        if j>entry and ts[j]-ts[j-1]>p['max_observation_gap_seconds']:gap=True
        qj=fs[j]['quotes'][symbol]
        if not 0<=ts[j]-(tape['exchange_times'][j][symbol] if 'exchange_times' in tape else instant(qj['exchange_at']).timestamp())<=p['max_quote_age_seconds']:gap=True;continue
        net=qj['bid']*(1-slip)*(1-fee)/(price*(1+fee))-1
        path.append((j,net))
        if touch is None:
            if net>=rr*risk:touch='TARGET';trigger=j
            elif net<=-risk:touch='STOP';trigger=j
    if not path:row['status']='NO_OUTCOME_QUOTES';return row
    row.update(mfe_net=max(x[1] for x in path),mae_net=min(x[1] for x in path),mfe_gross=max(fs[j]['quotes'][symbol]['bid']/q['ask']-1 for j,_ in path),mae_gross=min(fs[j]['quotes'][symbol]['bid']/q['ask']-1 for j,_ in path),touch=touch or 'NEITHER',gap_uncertain=gap)
    trigger=end if trigger is None else trigger
    exit_index=bisect.bisect_left(ts,ts[trigger]+delay)
    exit_rejection=None
    def feasible(j,k):
        return j<len(ts) and quote_ok(j,ts[k]+p['decision_latency_seconds']) and qty<=fs[j]['quotes'][symbol]['bid_size']*p['max_book_participation']
    if not feasible(exit_index,trigger):
        exit_rejection='EXIT_DEPTH_OR_QUOTE_REJECTED'
        trigger=end;exit_index=bisect.bisect_left(ts,ts[end]+delay)
    if not feasible(exit_index,trigger):
        row['status']='OPEN_UNLIQUIDATED';exit_index=len(ts)-1
    else:row['status']='CLOSED'
    sell=fs[exit_index]['quotes'][symbol]['bid']*(1-slip);gross=qty*sell;fees=notional*fee+gross*fee
    pnl=gross*(1-fee)-spend
    peak=max(equity,prior_peak or equity);dd=0.
    for j in range(entry,exit_index+1):
        mark=equity-spend+qty*fs[j]['quotes'][symbol]['bid']*(1-slip)*(1-fee)
        peak=max(peak,mark);dd=max(dd,1-mark/peak)
    row.update(exit_index=exit_index,exit_trigger_index=trigger,exit_price=sell,exit_rejection=exit_rejection,
        fees=fees,slippage=qty*(fs[entry]['quotes'][symbol]['ask']+fs[exit_index]['quotes'][symbol]['bid'])*slip,
        pnl=pnl,net_return=pnl/spend,log_growth=math.log1p(pnl/equity),intratrade_drawdown=dd,
        peak_equity=peak,trade_gap_uncertain=any(ts[j]-ts[j-1]>p['max_observation_gap_seconds'] for j in range(entry+1,exit_index+1)),
        mark_or_fill='FILL' if row['status']=='CLOSED' else 'FINAL_BID_MARK_NOT_FILL')
    return row


def percentile(values,q):
    if not values:return None
    v=sorted(values);return v[round((len(v)-1)*q)]


def summarize(records,rr):
    closed=[x for x in records if x['status']=='CLOSED'];clean=[x for x in closed if not x['gap_uncertain']]
    touches={s:sum(x['touch']==s for x in clean) for s in ('TARGET','STOP','NEITHER')}
    binary=touches['TARGET']+touches['STOP']
    return dict(attempts=len(records),closed=len(closed),clean_observed=len(clean),right_censored=sum(x['status']=='RIGHT_CENSORED' for x in records),
        entry_depth_rejected=sum(x['status']=='ENTRY_DEPTH_REJECTED' for x in records),exit_rejected=sum(bool(x.get('exit_rejection')) for x in records),
        uncertain_or_open=sum(x['status']=='OPEN_UNLIQUIDATED' or x.get('gap_uncertain',False) for x in records),touches=touches,
        binary_break_even=1/(rr+1),target_first_rate=touches['TARGET']/binary if binary else None,
        actual_positive_return_rate=sum(x['net_return']>0 for x in clean)/len(clean) if clean else None,
        mean_net_return=sum(x['net_return'] for x in clean)/len(clean) if clean else None,
        mean_log_growth=sum(x['log_growth'] for x in clean)/len(clean) if clean else None,
        mfe_net_quantiles={str(q):percentile([x['mfe_net'] for x in clean],q) for q in (.5,.9,.99)},
        mae_net_quantiles={str(q):percentile([x['mae_net'] for x in clean],q) for q in (.5,.1,.01)},
        hindsight_positive_mfe=sum(x['mfe_net']>0 for x in clean),sample_warning='OVERLAPPING_OUTCOMES_NOT_INDEPENDENT')


def analyze(tape,p,grid):
    ts=tape['times'];split=ts[0]+(ts[-1]-ts[0])*p['split_fraction'];opps=[];strategies=[]
    for g in grid:
        i=g['index']
        for s,f in g['features']['symbols'].items():
            if f['windows']['300']['status']!='AVAILABLE':continue
            risk=max(p['stop_vol_floor_fraction'],f['windows']['300']['realized_vol'])+f['round_trip_cost_bps']/10000
            for horizon in p['horizons_seconds']:
                for rr in p['net_reward_risk']:
                    r=simulate(tape,p,i,s,horizon,rr,risk)
                    r['partition']='DISCOVERY' if ts[i]+horizon+2.5<split else 'VALIDATION' if ts[i]>=split else 'PURGED_CROSS_SPLIT'
                    opps.append(r)
    for name in p['strategies']:
        for horizon in p['horizons_seconds']:
            for rr in p['net_reward_risk']:
                for partition,lo,hi in [('DISCOVERY',ts[0],split),('VALIDATION',split,ts[-1])]:
                    partition_tape={k:v[:bisect.bisect_left(ts,hi)] for k,v in tape.items()} if partition=='DISCOVERY' else tape
                    equity=p['capital_usdt'];peak=equity;dd=0.;busy=-1;trades=[];lossrun=0;maxloss=0;halted=False
                    for g in grid:
                        i=g['index']
                        if halted or i<=busy or ts[i]<lo or ts[i]+horizon+2.5>=hi:continue
                        eligible=[(abs(f['windows']['300'].get('return_fraction',0)),s,f) for s,f in g['features']['symbols'].items() if signal(f,name)]
                        if not eligible:continue
                        _,s,f=max(eligible,key=lambda x:(x[0],x[1]))
                        risk=max(p['stop_vol_floor_fraction'],f['windows']['300']['realized_vol'])+f['round_trip_cost_bps']/10000
                        r=simulate(partition_tape,p,i,s,horizon,rr,risk,equity,peak);trades.append(r)
                        if r.get('entry_index') is None:continue
                        busy=r.get('exit_index',len(ts)-1)
                        if 'pnl' not in r:halted=True;continue
                        # Cross-trade peak and intratrade drawdown are both retained.
                        dd=max(dd,r['intratrade_drawdown'])
                        equity+=r['pnl'];peak=max(peak,r['peak_equity'],equity);dd=max(dd,1-equity/peak)
                        lossrun=lossrun+1 if r['pnl']<0 else 0;maxloss=max(maxloss,lossrun)
                        halted=r['status']!='CLOSED' or dd>=p['max_drawdown_fraction']
                    clean=[r for r in trades if r['status']=='CLOSED' and not r['gap_uncertain']]
                    growth=math.log(equity/p['capital_usdt'])
                    uncertain=any(r.get('gap_uncertain') or r['status']=='OPEN_UNLIQUIDATED' for r in trades)
                    verdict='INSUFFICIENT'
                    if partition=='VALIDATION' and not uncertain:
                        if len(clean)>=p['candidate_min_validation_trades'] and growth>0:verdict='CANDIDATE'
                        elif len(clean)>=p['reject_min_validation_trades'] and growth<=0:verdict='REJECT'
                    strategies.append(dict(strategy=name,horizon=horizon,rr=rr,partition=partition,trades=trades,
                        equity=equity,pnl=equity-p['capital_usdt'],log_growth=growth,mean_log_growth=growth/len(clean) if clean and len(clean)==sum('pnl' in x for x in trades) else None,
                        max_drawdown=dd,max_consecutive_losses=maxloss,halted=halted,verdict=verdict,summary=summarize(trades,rr)))
    groups=[]
    for s in p['symbols']:
        for horizon in p['horizons_seconds']:
            for rr in p['net_reward_risk']:
                records=[r for r in opps if r['symbol']==s and r['horizon']==horizon and r['rr']==rr]
                groups.append(dict(symbol=s,horizon=horizon,rr=rr,independent_time_blocks_upper_bound=int((ts[-1]-ts[0])/horizon),**summarize(records,rr)))
    return dict(opportunities=opps,opportunity_groups=groups,strategies=strategies)


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--protocol',type=Path,default=Path('configs/opportunity_v1.json'));a=p.parse_args()
    repo=Path(__file__).resolve().parents[2];out=a.output.resolve();source=a.source.resolve()
    if out.exists() or out.is_relative_to(repo) or out.is_relative_to(source):raise ValueError('NEW_PRIVATE_OUTPUT_REQUIRED')
    protocol=json.loads(a.protocol.read_text());out.mkdir();(out/'frozen-protocol.json').write_bytes(canonical(protocol))
    start=time.perf_counter();tape=load_tape(source);grid=feature_grid(tape,protocol);feature_elapsed=time.perf_counter()-start
    result=analyze(tape,protocol,grid)
    result['metadata']=dict(classification='REUSED_24H_OFFLINE_NOT_AI',protocol_sha256=sha256(protocol),frames=len(tape['frames']),start=tape['frames'][0]['received_at'],end=tape['frames'][-1]['received_at'],grid_points=len(grid),feature_load_seconds=feature_elapsed,total_seconds=time.perf_counter()-start,external_calls=0)
    (out/'features.json').write_bytes(canonical(grid));(out/'result.json').write_bytes(canonical(result))
    print(json.dumps(result['metadata']))

if __name__=='__main__':main()
