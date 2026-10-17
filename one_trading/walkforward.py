"""New-data chronological experiment using the recovered v0.3.1 model/broker.

This is not reproduction of omitted experiments or a fundamentals strategy.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
from .historical_data import load_histories, ms, sha, write_json, verify_derived
from .learning import HistoryPanel, MaturedRidge, feature, clip
from .observer import DAY, STABLE_BASES, EXCLUDED_BASES, json_text
from .portfolio import Book, make_plan, validate_config
from .replay import verify_archive

PROTOCOL = {
    'id':'ONE-new-archive-walkforward-v1',
    'initial_warmup':'2018; test begins 2019-01-01 UTC',
    'universe':'Binance archived USDT spot, top 40 by trailing 30 completed-day mean quote volume at signal close',
    'history_requirement':'240 consecutive UTC candles, positive current volume, no known identity boundary inside window',
    'model':'unchanged v0.3.1 price-only Ridge, matured 28-day labels, 540-day rolling training, refit every 7 days',
    'execution':'signal D close observed D+1, fills earliest D+2 open; no retroactive same-open entry',
    'cost_cases':{'base':{'fee_rate':.001,'slippage_rate':.001},'stress':{'fee_rate':.002,'slippage_rate':.003}},
    'missing_market':'fail mode rejects held missing bars; zero mode writes off full cost basis after first missing/nontrading UTC day, no synthetic sale',
    'folds':'continuous 2019+ and independent calendar-year accounts; annual folds are NOT stitched into an investable equity curve',
    'regime':'prior-day BTC close vs SMA200 and trailing 28-day return: both positive=BULL, both negative=BEAR, otherwise TRANSITION',
    'benchmark':'BTC buy at same test-start open and liquidate same test-end close, same fee/slippage; no cash yield',
    'tuning':'no parameter search; fixed protocol before performance inspection',
    'scope':'historical walk-forward, archive-availability universe; incomplete listing notices; NOT untouched OOS, whole-market coverage or alpha approval',
}

# Publicly documented ticker reuse. No old/new LUNA price splice in features/labels.
IDENTITY_BOUNDARIES = {'LUNAUSDT':[ms('2022-05-31')]}
EXCLUDED = STABLE_BASES | EXCLUDED_BASES | {'1INCHUP','1INCHDOWN','SXPUP','SXPDOWN',
    'AUD','BRL','BIDR','IDRT','NGN','RUB','UAH','TRY','ZAR','BKRW','GBP'}
EXCLUDED |= {base+suffix for base in ['1INCH','AAVE','ADA','BCH','BNB','BTC','DOT',
    'EOS','ETH','FIL','LINK','LTC','SUSHI','SXP','TRX','UNI','XLM','XRP','XTZ','YFI']
    for suffix in ['UP','DOWN']}
EXCLUDED |= {'BULL','BEAR','ETHBULL','ETHBEAR','EOSBULL','EOSBEAR','XRPBULL','XRPBEAR'}


def iso(t): return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()


class HistoricalPanel(HistoryPanel):
    def __init__(self,histories,config):
        super().__init__(histories,config)
        self.segments={};self.liquidity={};self.day_cache={};self.row_cache={}
        for s,bs in histories.items():
            segments=[];liquidity=[];begin=0;vol=[]
            for i,b in enumerate(bs):
                if i and (b.open_ms!=bs[i-1].open_ms+DAY or
                          any(bs[i-1].open_ms<t<=b.open_ms for t in IDENTITY_BOUNDARIES.get(s,[]))):
                    begin=i;vol=[]
                segments.append(begin);vol.append(b.quote_volume)
                liquidity.append(statistics.mean(vol[-30:]))
            self.segments[s]=segments;self.liquidity[s]=liquidity

    def eligible(self,s,day):
        i=self.index[s].get(day)
        return (s!='BTCUSDT' and s.removesuffix('USDT') not in EXCLUDED and
                s not in self.config.get('excluded_symbols',[]) and i is not None and
                i-self.segments[s][i]+1>=240 and self.h[s][i].volume>0)

    def row(self,s,day):
        key=(s,day)
        if key not in self.row_cache:
            i=self.index.get(s,{}).get(day);bi=self.index['BTCUSDT'].get(day)
            r=None
            if i is not None and bi is not None and i-self.segments[s][i]+1>=240:
                r=feature(s,self.h[s][max(self.segments[s][i],i-239):i+1],self.h['BTCUSDT'][max(0,bi-239):bi+1])
            self.row_cache[key]=r
        return self.row_cache[key]

    def features_on(self,day):
        if day not in self.day_cache:
            pool=[s for s in self.h if self.eligible(s,day)]
            pool.sort(key=lambda s:(-self.liquidity[s][self.index[s][day]],s))
            self.day_cache[day]=[r for s in pool[:self.config['universe_limit']] if (r:=self.row(s,day))]
        return self.day_cache[day]

    def matured_group(self,outcome_day):
        c=self.config; signal=outcome_day-(c['horizon_days']+1)*DAY; entry=signal+2*DAY
        be,bx=self.bar('BTCUSDT',entry),self.bar('BTCUSDT',outcome_day)
        if not be or not bx:return []
        btc_log=math.log(bx.close/be.open)
        cost=math.log((1+c['slippage_rate'])*(1+c['fee_rate'])/((1-c['slippage_rate'])*(1-c['fee_rate'])))
        out=[]
        for r in self.features_on(signal):
            s=r['symbol'];e=self.bar(s,entry)
            if not e or e.volume<=0:continue # no entry could execute
            dead=any((b:=self.bar(s,d)) is None or b.volume<=0 for d in range(entry,outcome_day+1,DAY))
            dead=dead or any(entry<t<=outcome_day for t in IDENTITY_BOUNDARIES.get(s,[]))
            # Preserve terminated labels instead of retaining only future survivors.
            lr=-100. if dead else math.log(self.bar(s,outcome_day).close/e.open)
            out.append({'x':r['x'],'ys':[clip(lr-cost,-1,1),clip(lr-btc_log-cost,-1,1)]})
        return out


def writeoff(book,s,ts):
    p=book.positions.pop(s)
    book.realized_pnl-=p.cost_basis
    return {'symbol':s,'ts_ms':ts,'cost_basis_lost':p.cost_basis,'quantity':p.qty,
            'proceeds':0.,'kind':'MISSING_MARKET_FULL_WRITEOFF_NOT_A_FILL'}


def regime(btc,index):
    if index<200:return 'WARMUP'
    past=btc[index-200:index];last=past[-1].close
    above=last>=statistics.mean(b.close for b in past)
    rising=last>=btc[index-29].close
    return 'BULL' if above and rising else 'BEAR' if not above and not rising else 'TRANSITION'


def metrics(values,initial):
    peak=initial;mdd=0.
    for v in values:
        peak=max(peak,v);mdd=min(mdd,v/peak-1)
    return {'total_return':values[-1]/initial-1,'max_drawdown':mdd}


def run(panel,config,start,end,missing='zero'):
    validate_config(config);panel.config=config
    btc=panel.h['BTCUSDT'];bars_test=[b for b in btc if start<=b.open_ms<end]
    if not bars_test or bars_test[0].open_ms!=start or bars_test[-1].open_ms!=end-DAY:
        raise ValueError('test_period_missing_btc_boundary')
    if any(b.open_ms!=a.open_ms+DAY for a,b in zip(btc,btc[1:])):raise ValueError('BTC_calendar_gap')
    model=MaturedRidge(config);book=Book(config['initial_cash']);pending=[];prior={}
    equity=[];audits=[];losses=[];notes=Counter();state_counts=Counter();first_ready=None
    benchmark_qty=config['initial_cash']/(bars_test[0].open*(1+config['slippage_rate'])*(1+config['fee_rate']))
    for idx,b in enumerate(btc):
        day=b.open_ms
        if day>=end:break
        # Only labels inside rolling training window are relevant at test start.
        if day<start-config['training_window_days']*DAY-2*DAY:continue
        bars={s:bar for s in panel.h if (bar:=panel.bar(s,day)) is not None and bar.volume>0}
        opens={s:x.open for s,x in bars.items()};closes={s:x.close for s,x in bars.items()}
        if day>=start:
            missing_held=set(book.positions)-set(bars)
            missing_held.update(s for s in book.positions if day in IDENTITY_BOUNDARIES.get(s,[]))
            if missing_held and missing=='fail':raise ValueError('held_market_missing:'+','.join(sorted(missing_held)))
            # Retain previous observed mark for opening risk checks; never invent a fill.
            open_marks={**{s:prior[s] for s in missing_held},**opens}
            book.begin_day(day,prior or opens)
            # A missing full daily candle is known only at day end. No orders on it.
            for s in missing_held:open_marks.pop(s,None)
            if not missing_held:
                book.monitor_quotes(opens,day,config)
                for p in list(pending):
                    if p['execute_after_ms']<=day:
                        notes.update(book.apply_plan(p,opens,day,config));pending.remove(p)
                book.monitor_bar(bars,b.close_ms,config)
            else:
                # Quarantine entire day's execution if any held market is unobservable.
                # This is a deliberately pessimistic stress convention, not a live broker.
                for s in sorted(missing_held):losses.append(writeoff(book,s,b.close_ms))
                pending=[];notes['MISSING_DAY_EXECUTION_QUARANTINED']+=1
                book.monitor_bar(bars,b.close_ms,config)
            eq=book.equity(closes);book.peak=max(book.peak,eq)
            equity.append({'ts_ms':b.close_ms,'equity':eq,'cash':book.cash,
                           'exposure':(eq-book.cash)/eq,'btc_equity':benchmark_qty*b.close,
                           'regime':regime(btc,idx),'drawdown':eq/book.peak-1})
        rows=panel.matured_group(day);model.learn(b.close_ms,rows,b.close_ms)
        if model.fit(b.close_ms):
            audit=model.metadata()
            if audit['max_label_close_ms']>audit['fit_ms']:raise AssertionError('future_label')
            if day>=start-2*DAY:audits.append(audit)
        if day>=start-2*DAY:
            fs=list(panel.features_on(day));seen={r['symbol'] for r in fs}
            for s in book.positions:
                if s not in seen:
                    r=panel.row(s,day)
                    if r is None:raise ValueError('held_features_unavailable:'+s)
                    fs.append(r)
            if fs:
                plan=make_plan(fs,model,book,closes,b.close_ms+1,config);pending.append(plan)
                state_counts.update(r['state'] for r in plan['decisions'])
                if model.weights is not None and first_ready is None:first_ready=plan['execute_after_ms']
        prior=closes
    book.liquidate(prior,bars_test[-1].close_ms,config,'END_OF_TEST_LIQUIDATION')
    equity[-1].update(equity=book.cash,cash=book.cash,exposure=0.,drawdown=book.cash/book.peak-1)
    equity[-1]['btc_equity']*= (1-config['slippage_rate'])*(1-config['fee_rate'])
    initial=config['initial_cash'];summary=metrics([r['equity'] for r in equity],initial)
    benchmark=metrics([r['btc_equity'] for r in equity],initial)
    if abs(book.realized_pnl-(book.cash-initial))>1e-6:raise AssertionError('PnL_reconciliation')
    # Regime returns compound only classified daily returns; not contiguous holding periods.
    buckets={};last_eq=last_btc=initial
    for r in equity:
        g=buckets.setdefault(r['regime'],{'days':0,'strategy_factor':1.,'btc_factor':1.})
        g['days']+=1;g['strategy_factor']*=r['equity']/last_eq;g['btc_factor']*=r['btc_equity']/last_btc
        last_eq,last_btc=r['equity'],r['btc_equity']
    by_regime={k:{'days':v['days'],'strategy_compounded_selected_days':v['strategy_factor']-1,
                  'btc_compounded_selected_days':v['btc_factor']-1} for k,v in buckets.items()}
    summary.update(start=iso(start),end_exclusive=iso(end),btc=benchmark,
                   excess_return_percentage_points=100*(summary['total_return']-benchmark['total_return']),
                   final_cash=book.cash,fees_paid=book.total_fees,
                   slippage_cost=sum(r['quantity']*abs(r['fill_price']-r['reference_price']) for r in book.fills),
                   fill_count=len(book.fills),turnover_notional=sum(r['notional'] for r in book.fills),
                   average_daily_exposure=statistics.mean(r['exposure'] for r in equity),
                   latched_drawdown_halt=book.halted,writeoff_count=len(losses),
                   writeoff_cost_basis=sum(r['cost_basis_lost'] for r in losses),model_refits=len(audits),
                   first_model_eligible_execution=iso(first_ready) if first_ready else None,
                   regime=by_regime,execution_notes=dict(notes),decisions_by_state=dict(state_counts))
    return {'summary':summary,'equity':equity,'fills':book.fills,'writeoffs':losses,'model_audit':audits,'config':config}


def experiment(archive,output,config,start,end,missing):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    manifest={'protocol':PROTOCOL,'config':config,'start':iso(start),'end_exclusive':iso(end),'missing_policy':missing,
              'code_sha256':{str(p):sha(p.read_bytes()) for p in sorted(Path('one_trading').glob('*.py'))},
              'registered_at_utc':datetime.now(timezone.utc).isoformat()}
    write_json(output/'protocol.json',manifest)
    # Raw snapshots must validate before any performance calculation.
    evidence=verify_archive(Path(archive));evidence.update(verify_derived(archive));h,coverage=load_histories(archive)
    write_json(output/'dataset.json',{'archive':evidence,'coverage':coverage})
    if len(h.get('BTCUSDT',[]))<360:raise ValueError('insufficient_BTC_history')
    if min(b.open_ms for b in h['BTCUSDT'])>start-360*DAY:raise ValueError('insufficient_warmup')
    panel=HistoricalPanel(h,config);results={}
    folds=[('continuous',start,end)]
    for year in range(datetime.fromtimestamp(start/1000,timezone.utc).year,datetime.fromtimestamp((end-1)/1000,timezone.utc).year+1):
        folds.append((str(year),max(start,ms(f'{year}-01-01')),min(end,ms(f'{year+1}-01-01'))))
    for case,costs in PROTOCOL['cost_cases'].items():
        c={**config,**costs};results[case]={}
        for name,a,z in folds:
            t=time.monotonic();result=run(panel,c,a,z,missing)
            write_json(output/f'{case}_{name}.json',result)
            results[case][name]=result['summary']
            print(json.dumps({'case':case,'fold':name,'seconds':round(time.monotonic()-t,1),'summary':result['summary']}),flush=True)
    write_json(output/'comparison.json',results)
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',required=True);p.add_argument('--output',required=True);p.add_argument('--config',default='config.json')
    p.add_argument('--start',default='2019-01-01');p.add_argument('--end',default='2026-10-01')
    p.add_argument('--missing',choices=['fail','zero'],default='fail')
    a=p.parse_args();experiment(a.archive,a.output,json.loads(Path(a.config).read_text()),ms(a.start),ms(a.end),a.missing)

if __name__=='__main__':main()
