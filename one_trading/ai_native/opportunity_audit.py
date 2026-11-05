"""Independent arithmetic/chronology verifier; does not call the study simulator.

This verifies recorded outcomes, not profitability or an exhaustive signal search.
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
    def check(r):
        nonlocal checked
        i=r['decision_index'];s=r['symbol'];entry=r['entry_index'];exit=r['exit_index']
        if entry is None:return
        if entry!=bisect.bisect_left(times,times[i]+delay):raise ValueError('ENTRY_CHRONOLOGY')
        if exit is None:return
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
            end=bisect.bisect_left(times,times[i]+r['horizon']);values=[];touch='NEITHER'
            for j in range(entry,end+1):
                quote=frames[j]['quotes'][s]
                age=times[j]-datetime.fromisoformat(quote['exchange_at']).timestamp()
                if not 0<=age<=p['max_quote_age_seconds']:continue
                value=quote['bid']*(1-slip)*(1-fee)/(price*(1+fee))-1
                values.append(value)
                if touch=='NEITHER':
                    if value>=r['rr']*r['risk']:touch='TARGET'
                    elif value<=-r['risk']:touch='STOP'
            cache[key]=(max(values),min(values),touch)
        mfe,mae,touch=cache[key];near(r['mfe_net'],mfe);near(r['mae_net'],mae)
        if r['touch']!=touch:raise ValueError('TOUCH_ORDER')
        checked+=1
    for r in result['opportunities']:check(r)
    for strategy in result['strategies']:
        equity=p['capital_usdt'];last=-1;peak=equity;dd=0
        for r in strategy['trades']:
            if r['decision_index']<=last:raise ValueError('OVERLAPPING_POSITIONS')
            near(r['start_equity'],equity);check(r)
            if r.get('exit_index') is None:continue
            if strategy['partition']=='DISCOVERY' and times[r['exit_index']]>=times[0]+(times[-1]-times[0])*p['split_fraction']:raise ValueError('CROSS_SPLIT_LEAK')
            for j in range(r['entry_index'],r['exit_index']+1):
                mark=equity-r['entry_spend']+r['quantity']*frames[j]['quotes'][r['symbol']]['bid']*(1-slip)*(1-fee)
                peak=max(peak,mark);dd=max(dd,1-mark/peak)
            equity+=r['pnl'];last=r['exit_index']
        near(strategy['equity'],equity);near(strategy['pnl'],equity-p['capital_usdt'])
        near(strategy['log_growth'],math.log(equity/p['capital_usdt']));near(strategy['max_drawdown'],dd)
    return dict(status='PASS_RECORDED_OUTCOMES',checked_positions=checked,scope='Independent causal risk, MFE/MAE, touch order, delays, depth, fees, arithmetic, overlap, partition and drawdown. Raw source audit required separately; not alpha proof.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--study',type=Path,required=True);parser.add_argument('--source',type=Path,required=True);a=parser.parse_args()
    result=json.loads((a.study/'result.json').read_bytes());p=json.loads((a.study/'frozen-protocol.json').read_bytes())
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
