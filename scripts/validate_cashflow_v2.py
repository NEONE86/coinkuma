"""Separate-process ledger, source-rule, chronology and reproduction audit."""
from __future__ import annotations
import argparse
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.validate_cashflow_alpha import (close, recompute, check_reported_summary,
    audit_market_fills, block_bootstrap)
from one_trading.historical_data import load_histories, ms, write_json
from one_trading.observer import DAY

A='relative_strength_recovery'
B='established_trend_controlled_pullback'


class SourceRules:
    """Rebuild membership and predicates without the strategy feature functions."""
    def __init__(self, histories):
        from one_trading.walkforward import EXCLUDED, IDENTITY_BOUNDARIES
        self.h=histories
        self.index={s:{b.open_ms:i for i,b in enumerate(bs)} for s,bs in histories.items()}
        by_day=defaultdict(list)
        for s,bs in histories.items():
            if s=='BTCUSDT' or s.removesuffix('USDT') in EXCLUDED:continue
            consecutive=0
            for i,b in enumerate(bs):
                reset=(i==0 or b.open_ms-bs[i-1].open_ms!=DAY or
                       any(bs[i-1].open_ms<t<=b.open_ms for t in IDENTITY_BOUNDARIES.get(s,[])))
                consecutive=1 if reset else consecutive+1
                if consecutive>=240 and b.volume>0:
                    q=sum(x.quote_volume for x in bs[i-29:i+1])/30
                    if q>=1_000_000:by_day[b.open_ms].append((s,q))
        self.universes={d:[s for s,_ in sorted(v,key=lambda x:(-x[1],x[0]))[:80]] for d,v in by_day.items()}
        self.candidates={}

    def predicate(self,s,day,strategy):
        i=self.index[s][day];bi=self.index['BTCUSDT'][day]
        bs=self.h[s][i-239:i+1];btc=self.h['BTCUSDT'][bi-239:bi+1]
        c=[b.close for b in bs];price=c[-1]
        avg=lambda xs:sum(xs)/len(xs)
        tr=[max(bs[j].high-bs[j].low,abs(bs[j].high-c[j-1]),abs(bs[j].low-c[j-1])) for j in range(200,240)]
        stop=price-2.5*avg(tr[-14:])
        if not (btc[-1].close>avg([b.close for b in btc[-200:]]) and .03<=(price-stop)/price<=.18):return False
        r7=price/c[-8]-1;r28=price/c[-29]-1
        if strategy==A:
            sm=lambda j:avg(c[j-49:j+1])
            below=sum(c[j]<sm(j) for j in range(205,233))
            cross=any(c[j-1]<sm(j-1) and c[j]>sm(j) for j in range(233,240))
            return (below>=14 and cross and price>sm(239)
                    and .10<=1-price/max(b.high for b in bs[-180:])<=.60
                    and r28-(btc[-1].close/btc[-29].close-1)>=.03
                    and r7<=.12 and 0<=r28<=.30 and avg(tr[:-10])>0
                    and avg(tr[-10:])/avg(tr[:-10])<=.90 and c[-1]>c[-8])
        return (avg(c[-50:])>avg(c[-200:]) and avg(c[-50:])>avg(c[-70:-20])
                and .08<=1-price/max(b.high for b in bs[-60:])<=.25
                and .95<=price/avg(c[-20:])<=1.03 and price>c[-2] and price>bs[-1].open
                and r7<=.08 and -.15<=r28<=.25
                and (price/c[-91]-1)-(btc[-1].close/btc[-91].close-1)>0)

    def check(self,result,holdings):
        strategy=result['config']['strategy'];checked=0
        for p in result['plans']:
            day=p['signal_day_ms']
            if p['universe']!=self.universes.get(day,[]):raise AssertionError('historical_universe_mismatch')
            actual=holdings.get(day,{})
            if p['position_context']!=actual:raise AssertionError('signal_position_context_mismatch')
            key=(strategy,day)
            if key not in self.candidates:
                self.candidates[key]={s for s in p['universe'] if self.predicate(s,day,strategy)}
            expected=self.candidates[key]-set(actual)
            reported={d['symbol'] for d in p['decisions'] if d['state'] in {'ENTER','WAIT_CAPACITY'}}
            if expected!=reported:raise AssertionError(f'candidate_rules_mismatch:{strategy}:{day}:{expected^reported}')
            if p['opportunity_count']!=len(self.candidates[key]):raise AssertionError('opportunity_count_mismatch')
            for t in p['targets']:
                if t['symbol'] not in actual:
                    s=t['symbol'];i=self.index[s][day];bs=self.h[s][i-14:i+1]
                    tr=[max(b.high-b.low,abs(b.high-bs[j-1].close),abs(b.low-bs[j-1].close)) for j,b in enumerate(bs) if j]
                    close(t['stop'],bs[-1].close-2.5*sum(tr)/14,'initial_ATR_stop')
                elif t['allow_add']:raise AssertionError('held_target_allows_add')
            checked+=len(p['universe'])
        return dict(source_universe_and_rules=True,dates=len(result['plans']),symbol_dates=checked)


def chronology(result):
    by_due=defaultdict(list)
    for p in result['plans']:
        d=p['signal_day_ms']
        if result['config']['strategy']==A and (d//DAY+3)%7!=6:raise AssertionError('A_non_sunday')
        if p['observed_ms']!=d+DAY or p['execute_after_ms']!=d+2*DAY:raise AssertionError('delay_violation')
        if any(x['max_input_close_ms']>=p['observed_ms'] for x in p['decisions']):raise AssertionError('future_input')
        by_due[p['execute_after_ms']].append(p)
    for f in result['fills']:
        if f['side']=='BUY':
            if f['reason']!='OPEN_PROBE':raise AssertionError('adds_forbidden')
            if not any(any(t['symbol']==f['symbol'] and t['allow_add'] for t in p['targets']) for p in by_due[f['ts_ms']]):
                raise AssertionError('BUY_without_delayed_plan')
    return dict(chronology=True,no_adds=True,plans=len(result['plans']))


def daily_ledger(result,histories):
    """Mark every day from source closes, independent of reported equity."""
    indices={s:{b.open_ms:b for b in bs} for s,bs in histories.items()}
    c=result['config'];cash=c['initial_cash'];positions={};holdings={};events=defaultdict(list)
    for i,f in enumerate(result['fills']):events[f['ts_ms']//DAY*DAY].append((f['ts_ms'],i,'fill',f))
    for i,w in enumerate(result['writeoffs']):events[w['ts_ms']//DAY*DAY].append((w['ts_ms'],len(result['fills'])+i,'writeoff',w))
    due={p['execute_after_ms']:p for p in result['plans']}
    max_entry_risk=max_daily_risk=max_exposure=0.;max_count=0;last_close={}
    for row in result['equity']:
        day=row['ts_ms']//DAY*DAY
        plan=due.get(day)
        if plan:
            for t in plan['targets']:
                s=t['symbol'];p=positions.get(s);bar=indices.get(s,{}).get(day)
                if p and bar and plan['position_context'].get(s)==p['opened_ms']:
                    p['stop']=max(p['stop'],min(t['stop'],bar.open*.999))
        before_terminal=None
        for ts,_,kind,f in sorted(events[day]):
            s=f['symbol']
            if kind=='writeoff':
                p=positions.pop(s)
                close(p['qty'],f['quantity'],'writeoff_qty')
                continue
            if f['reason']=='END_OF_TEST_LIQUIDATION' and before_terminal is None:
                before_terminal={s:p['opened_ms'] for s,p in positions.items()}
            qty=f['quantity'];notional=qty*f['fill_price'];fee=f['fee']
            close(fee,notional*c['fee_rate'],'fee_rate')
            close(f['fill_price'],f['reference_price']*(1+c['slippage_rate'] if f['side']=='BUY' else 1-c['slippage_rate']),'slippage_rate')
            if f['side']=='BUY':
                if s in positions:raise AssertionError('unregistered_add')
                t=next(t for t in due[day]['targets'] if t['symbol']==s)
                marks={k:indices.get(k,{}).get(day).open if indices.get(k,{}).get(day) else last_close[k] for k in positions}
                before=cash+sum(p['qty']*marks[k] for k,p in positions.items())
                individual=qty*(f['fill_price']*(1+c['fee_rate'])-t['stop']*(1-c['slippage_rate'])*(1-c['fee_rate']))/before
                if individual>.005+1e-8:raise AssertionError('individual_entry_risk')
                cash-=notional+fee
                positions[s]=dict(qty=qty,opened_ms=ts,stop=t['stop'])
                marks[s]=indices[s][day].open
                eq=cash+sum(p['qty']*marks[k] for k,p in positions.items())
                risk=sum(p['qty']*max(0.,marks[k]*(1+c['fee_rate'])-p['stop']*(1-c['slippage_rate'])*(1-c['fee_rate'])) for k,p in positions.items())/eq
                gross=sum(p['qty']*marks[k] for k,p in positions.items())/eq
                if risk>.03+1e-8 or gross>.95+1e-8 or len(positions)>8:raise AssertionError('aggregate_entry_limits')
                if any(p['qty']*marks[k]/eq>.20+1e-8 for k,p in positions.items()):
                    # Appreciated existing positions can drift over weight; only
                    # the new entry's weight is constrained at this instant.
                    if positions[s]['qty']*marks[s]/eq>.20+1e-8:raise AssertionError('new_position_weight')
                max_entry_risk=max(max_entry_risk,risk)
            else:
                p=positions[s];p['qty']-=qty;cash+=notional-fee
                if p['qty'] < -1e-7*max(qty,1.):raise AssertionError('negative_inventory')
                if abs(p['qty'])<=1e-8*max(qty,1.):positions.pop(s)
            if cash < -1e-6:raise AssertionError('negative_cash')
        prices={s:indices[s][day].close for s in positions}
        eq=cash+sum(p['qty']*prices[s] for s,p in positions.items())
        close(cash,row['cash'],'daily_cash',c['initial_cash'])
        close(eq,row['equity'],'daily_source_marked_equity',c['initial_cash'])
        risk=sum(p['qty']*max(0.,prices[s]*(1+c['fee_rate'])-p['stop']*(1-c['slippage_rate'])*(1-c['fee_rate'])) for s,p in positions.items())/eq
        close(risk,row['open_risk_fraction'],'daily_open_risk')
        max_daily_risk=max(max_daily_risk,risk);max_exposure=max(max_exposure,(eq-cash)/eq)
        max_count=max(max_count,len(positions))
        holdings[day]=before_terminal if before_terminal is not None else {s:p['opened_ms'] for s,p in positions.items()}
        last_close={s:bs[day].close for s,bs in indices.items() if day in bs}
    return dict(all_daily_cash_inventory_marks=True,days=len(result['equity']),
                max_risk_after_entry=max_entry_risk,max_daily_close_open_risk=max_daily_risk,
                max_daily_close_exposure=max_exposure,max_daily_close_positions=max_count),holdings


def reproduce_and_prefix(result,histories):
    from one_trading.cashflow_alpha_v2 import ChallengerPanel,run
    c=result['config'];start=ms(result['summary']['start']);end=ms(result['summary']['end_exclusive'])
    repeated=run(ChallengerPanel(histories,c),c,start,end)
    if result!=repeated:raise AssertionError('full_reproduction_mismatch')
    del repeated
    cut=ms('2023-01-01')
    truncated={s:kept for s,bs in histories.items() if (kept:=[b for b in bs if b.open_ms<cut])}
    short=run(ChallengerPanel(truncated,c),c,start,cut,liquidate_end=False)
    for key,timekey in {'equity':'ts_ms','fills':'ts_ms','writeoffs':'ts_ms','plans':'signal_day_ms'}.items():
        if [x for x in result[key] if x[timekey]<cut]!=short[key]:raise AssertionError('future_prefix:'+key)
    return dict(full_reproduction_identical=True,future_data_deleted_at='2023-01-01',
                prefix_identical=True,prefix_days=len(short['equity']),prefix_fills=len(short['fills']))


def decision_for(comparison,monthly,bootstrap,controls,audited):
    s=comparison['stress']['continuous'];folds=[comparison['stress'][str(y)] for y in range(2019,2027)]
    stress_accounts=list(comparison['stress'].values())
    values=list(monthly.values());mean=statistics.mean(values)
    percentile=sum(x['mean_monthly_net_return']<mean for x in controls)/len(controls) if controls else None
    gates=dict(stress_pf=s['profit_factor'] is not None and s['profit_factor']>1.30,
        total_sample=s['trade_count']>=200,
        challenge_sample=comparison['stress']['challenge']['trade_count']>=50,
        expectancy=s['expectancy_per_closed_trade'] is not None and s['expectancy_per_closed_trade']>0,
        positive_calendar_folds=sum(x['total_return']>0 for x in folds)/8>=.60,
        max_drawdown=all(x['max_drawdown']>=-.15-1e-12 for x in stress_accounts),
        no_permanent_halt=all(not x['latched_drawdown_halt'] for x in stress_accounts),
        bootstrap_positive_lower=bootstrap['monthly_mean_95pct_interval'][0]>0,
        random_control=len(controls)==200 and percentile>=.975,
        monthly_growth=mean>=.01,
        positive_months=sum(v>0 for v in values)/len(values)>=.55,
        required_checks=audited)
    measured=[k for k,v in gates.items() if not v and k not in {'total_sample','challenge_sample','required_checks'}]
    decision='REJECT' if measured else ('PASS' if all(gates.values()) else 'NEEDS_MORE_EVIDENCE')
    if not audited:decision='NEEDS_MORE_EVIDENCE'
    return dict(decision=decision,gates=gates,failed_gates=[k for k,v in gates.items() if not v],
                stress_mean_monthly_return=mean,positive_calendar_fold_fraction=sum(x['total_return']>0 for x in folds)/8,
                control_percentile=percentile,control_count=len(controls),
                control_one_sided_monte_carlo_p=(1+sum(x['mean_monthly_net_return']>=mean for x in controls))/(len(controls)+1))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True);p.add_argument('--archive',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();run_dir=Path(a.run);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    histories,_=load_histories(a.archive);source=SourceRules(histories)
    comparison=json.loads((run_dir/'comparison.json').read_text(encoding='utf-8'))
    audits={};decisions={}
    for path in sorted(run_dir.glob('*__*.json')):
        if path.name.endswith('__controls.json'):continue
        result=json.loads(path.read_text(encoding='utf-8'));independent=recompute(result)
        check_reported_summary(result,independent)
        daycheck,holdings=daily_ledger(result,histories)
        checks={**chronology(result),**daycheck,**audit_market_fills(result,histories),**source.check(result,holdings)}
        entry=dict(checks=checks,metrics=independent['metrics'])
        if path.name.endswith('__continuous.json'):
            entry['reproduction']=reproduce_and_prefix(result,histories)
            entry['bootstrap']=block_bootstrap(independent['metrics']['monthly_returns'])
            write_json(out/(path.stem+'_campaign_audit.json'),independent)
        audits[path.stem]=entry
        write_json(out/'account_audits.json',audits)
        print(json.dumps(dict(phase='AUDITED',account=path.stem,days=checks['days'])),flush=True)
        del result,independent
    for strategy in (A,B):
        controls=json.loads((run_dir/f'{strategy}__controls.json').read_text(encoding='utf-8'))
        if [c['seed'] for c in controls]!=list(range(200)):raise AssertionError('control_seed_set')
        stress=audits[f'{strategy}__stress__continuous']
        decisions[strategy]=decision_for(comparison[strategy],stress['metrics']['monthly_returns'],stress['bootstrap'],controls,True)
    passed=[s for s,v in decisions.items() if v['decision']=='PASS']
    selected=sorted(passed,key=lambda s:(-decisions[s]['stress_mean_monthly_return'],abs(comparison[s]['stress']['continuous']['max_drawdown']),s))[:1]
    write_json(out/'decision.json',dict(strategies=decisions,selected=selected,
               audited_accounts=len(audits),external_investment_audit=False,live_orders=False))
    print(json.dumps(dict(phase='VALIDATION_COMPLETE',decisions=decisions,selected=selected)),flush=True)


if __name__=='__main__':main()
