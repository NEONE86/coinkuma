"""Cashflow Alpha v1: frozen, price-only early-base swing research.

Reuses the recovered spot broker and PR2 historical panel. This is not a
fundamental valuation model, futures simulation, or exchange execution adapter.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
from .observer import DAY, complete_weeks, complete_months, json_text
from .portfolio import Book, validate_config
from .walkforward import HistoricalPanel, IDENTITY_BOUNDARIES, iso, metrics, writeoff

PROTOCOL = {
    'id': 'ONE-Cashflow-Alpha-v1',
    'hypothesis': 'Non-extended liquid bases with improving relative strength can precede repricing; price-only proxy, no fundamental evidence connected.',
    'selection': 'Top 80 trailing 30-day mean quote volume; minimum 1m USDT/day; 240 consecutive daily bars; inherited stable/leveraged-token exclusions.',
    'signal': {'btc_above_sma200': True, 'close_above_sma50': True,
               'completed_week_return_4w_min': 0., 'completed_month_return_1m_min': -.10,
               'drawdown_180d_range': [.05,.55], 'base_width_56d_max': .65,
               'return_7d_max': .12, 'return_28d_range': [0.,.30],
               'extension_sma20_max': .10, 'relative_return_28d_min': 0.,
               'stop_distance_range': [.03,.18]},
    'ranking': 'relative 28d return + 0.02 * clip(7d/previous28d quote-volume acceleration - 1, -1, 2); ties symbol ascending',
    'cadence': 'Completed Sunday close, observed Monday 00:00 UTC; Tuesday open earliest fill (D+2). Weekly trailing/exit plans only.',
    'execution': 'Inherited spot Book, 3R half take-profit, stop-first daily bars, adverse open gaps, stale-plan identity protection, 3% entry-gap ceiling.',
    'risk': '0.5% per position, 3% aggregate open risk, 8 positions, 20% max position, 95% gross; 3% daily pause, 15% permanent drawdown halt.',
    'exit': 'Risk-off BTC, close below SMA50 after 7 days, 84-day time exit, trailing support stop. Extension alone does not exit a winner.',
    'adds': 'At least 14 days held and >=1 original entry R profit, non-extended; same per-position risk budget, no leverage.',
    'cost_cases': {'base': {'fee_rate': .001, 'slippage_rate': .001},
                   'stress': {'fee_rate': .002, 'slippage_rate': .003}},
    'funding': 'Exactly zero for unborrowed spot; no futures returns, leverage or historic funding inferred.',
    'walkforward': 'Frozen deterministic policy with rolling past-only features; no training or parameter fitting. Continuous account plus independent yearly accounts; never stitch resets.',
    'missing': 'Missing held bar is unexecutable at open; prior mark retained for opening equity, other markets execute. At close zero-recovery writeoff or diagnostic failure; no terminal-price recovery.',
    'limitations': 'Daily OHLC cannot prove intraday path, volume available at open, actual fills, order depth or historical publication timing. 3%/15% are triggers, not guaranteed loss caps. No tax or FX modeling.',
}


def default_config(base=None):
    if base is None:
        base=json.loads((Path(__file__).resolve().parents[1]/'config.json').read_text())
    c=deepcopy(base)
    c.update(version='ONE-Cashflow-Alpha-v1', initial_cash=10000., universe_limit=80,
             min_quote_volume=1_000_000., max_positions=8, max_position_weight=.20,
             max_gross_weight=.95, probe_risk=.005, confirmed_risk=.005,
             max_open_risk=.03, daily_loss_limit=.03, drawdown_halt=.15,
             min_hold_days=7, max_hold_days=84, profit_take_R=3.,
             notes='Frozen cashflow-alpha research policy; unborrowed spot only.')
    validate_config(c)
    return c


def signal_row(panel, symbol, day):
    """Compute one row from bars no later than the requested completed day."""
    cache=getattr(panel,'cashflow_row_cache',None)
    if cache is None: cache={}; panel.cashflow_row_cache=cache
    key=(symbol,day)
    if key in cache:return cache[key]
    i=panel.index.get(symbol,{}).get(day); bi=panel.index.get('BTCUSDT',{}).get(day)
    if i is None or bi is None or i-panel.segments[symbol][i]+1<240 or bi<239:
        cache[key]=None;return None
    bs=panel.h[symbol][i-239:i+1];btc=panel.h['BTCUSDT'][bi-239:bi+1]
    cs=[b.close for b in bs];last=cs[-1]
    weeks=complete_weeks(bs);months=complete_months(bs)
    if len(weeks)<5 or len(months)<2:
        cache[key]=None;return None
    trs=[max(b.high-b.low,abs(b.high-bs[j-1].close),abs(b.low-bs[j-1].close))
         for j,b in enumerate(bs) if j>0][-14:]
    atr=statistics.mean(trs)
    stop=min(b.low for b in bs[-20:])-.25*atr
    recent=statistics.mean(b.quote_volume for b in bs[-7:])
    old=statistics.mean(b.quote_volume for b in bs[-35:-7])
    accel=recent/old if old>0 else 1.
    rel=(last/cs[-29]-1)-(btc[-1].close/btc[-29].close-1)
    m={'last_close':last,'sma50':statistics.mean(cs[-50:]),
       'btc_risk_on':btc[-1].close>statistics.mean(b.close for b in btc[-200:]),
       'return_7d':last/cs[-8]-1,'return_28d':last/cs[-29]-1,
       'relative_return_28d':rel,'extension_sma20':last/statistics.mean(cs[-20:])-1,
       'drawdown_180d':1-last/max(b.high for b in bs[-180:]),
       'base_width_56d':max(b.high for b in bs[-56:])/min(b.low for b in bs[-56:])-1,
       'completed_week_return_4w':weeks[-1].close/weeks[-5].close-1,
       'completed_month_return_1m':months[-1].close/months[-2].close-1,
       'quote_volume_acceleration':accel, 'stop_distance':(last-stop)/last,
       'liquidity_30d':panel.liquidity[symbol][i]}
    r={'symbol':symbol,'signal_day_ms':day,'max_input_close_ms':max(bs[-1].close_ms,btc[-1].close_ms),
       'support_stop':stop,'score':rel+.02*max(-1.,min(2.,accel-1.)), 'metrics':m}
    cache[key]=r;return r


def entry_reasons(row):
    m=row['metrics']
    tests=[('BTC_RISK_OFF',m['btc_risk_on']),('BELOW_SMA50',m['last_close']>m['sma50']),
           ('WEEK_NOT_IMPROVING',m['completed_week_return_4w']>0),
           ('MONTH_TOO_WEAK',m['completed_month_return_1m']>-.10),
           ('NOT_EARLY_BASE',.05<=m['drawdown_180d']<=.55),
           ('BASE_TOO_WIDE',m['base_width_56d']<=.65),
           ('SURGE_7D',m['return_7d']<=.12),('MOMENTUM_28D',0<=m['return_28d']<=.30),
           ('EXTENDED_SMA20',m['extension_sma20']<=.10),
           ('NO_RELATIVE_STRENGTH',m['relative_return_28d']>0),
           ('STOP_DISTANCE',.03<=m['stop_distance']<=.18)]
    return [name for name,passed in tests if not passed]


def make_swing_plan(panel,book,day,config,prices):
    """Weekly plan; only completed Sunday information reaches Tuesday execution."""
    if datetime.fromtimestamp(day/1000,timezone.utc).weekday()!=6:
        raise ValueError('signals_require_completed_sunday')
    pool=[s for s in panel.h if panel.eligible(s,day)
          and panel.liquidity[s][panel.index[s][day]]>=config['min_quote_volume']]
    pool.sort(key=lambda s:(-panel.liquidity[s][panel.index[s][day]],s))
    universe=pool[:config['universe_limit']]
    rows={s:r for s in sorted(set(universe)|set(book.positions)) if (r:=signal_row(panel,s,day))}
    decisions=[];held=[];new=[];eq=book.equity(prices)
    for s in sorted(rows):
        r=rows[s];m=r['metrics'];p=book.positions.get(s);age=(day+DAY-p.opened_ms)/DAY if p else 0
        d={'symbol':s,'state':'WAIT','reasons':[],**r}
        if p:
            if not m['btc_risk_on'] or age>=config['max_hold_days'] or (age>=config['min_hold_days'] and m['last_close']<m['sma50']):
                d.update(state='EXIT',reasons=['RISK_OFF_TREND_OR_TIME']);decisions.append(d);continue
            extended=m['return_7d']>.12 or m['return_28d']>.30 or m['extension_sma20']>.10
            original_entry=next((f['fill_price'] for f in book.fills
                                 if f['symbol']==s and f['side']=='BUY' and f['ts_ms']==p.opened_ms),p.average_entry)
            confirmed=(age>=14 and m['last_close']>=original_entry+(original_entry-p.initial_stop))
            d['original_entry_price']=original_entry
            d['original_risk_per_unit']=original_entry-p.initial_stop
            allow=confirmed and not extended and not p.took_profit
            stop=max(p.stop,r['support_stop'])
            weight=min(config['max_position_weight'],config['confirmed_risk']/max((m['last_close']-stop)/m['last_close'],.03))
            if not allow: weight=min(weight,p.qty*m['last_close']/eq)
            held.append({'symbol':s,'target_weight':weight,'risk_fraction':.005,
                         'stop':stop,'reference_price':m['last_close'],'allow_add':allow,
                         'stage':'CONFIRMED' if confirmed else 'PROBE'})
            d.update(state='ADD_OR_HOLD' if allow else 'HOLD',reasons=['WINNER_TRAILING_SUPPORT'])
        else:
            reasons=entry_reasons(r)
            if reasons:d.update(state='REJECT',reasons=reasons)
            else:
                d.update(state='CANDIDATE',reasons=['BASE_RELATIVE_STRENGTH'])
                new.append((r['score'],s,{'symbol':s,'target_weight':min(config['max_position_weight'],.005/m['stop_distance']),
                     'risk_fraction':.005,'stop':r['support_stop'],'reference_price':m['last_close'],
                     'allow_add':True,'stage':'PROBE'}))
        decisions.append(d)
    # A held market with a reset/gap window is explicitly exited at next observable
    # execution. Its gap was already visible by this signal close.
    for s in sorted(set(book.positions)-set(rows)):
        decisions.append({'symbol':s,'state':'EXIT','reasons':['HISTORY_INVALIDATED'],
                          'signal_day_ms':day,'max_input_close_ms':day+DAY-1})
    new.sort(key=lambda v:(-v[0],v[1]));slots=max(0,config['max_positions']-len(held))
    targets=held+[v[2] for v in new[:slots]]
    selected={t['symbol'] for t in targets}
    for d in decisions:
        if d['state']=='CANDIDATE':d['state']='ENTER' if d['symbol'] in selected else 'WAIT_CAPACITY'
    total=sum(t['target_weight'] for t in targets)
    if total>config['max_gross_weight']:
        for t in targets:t['target_weight']*=config['max_gross_weight']/total
    due=day+2*DAY
    plan={'observed_ms':day+DAY,'signal_day_ms':day,'execute_after_ms':due,'expires_ms':due+DAY-1,
          'position_context':{s:p.opened_ms for s,p in book.positions.items()},
          'targets':targets,'decisions':decisions,'universe':universe,
          'fundamental_revaluation_evidence':'NOT_CONNECTED','mode':'EXPERIMENTAL_PAPER_PLAN'}
    plan['id']=hashlib.sha256(json_text(plan).encode()).hexdigest()
    return plan


def _observable_risk(book,prices,available,ts,config):
    reason=book.risk_trigger(ts,prices,config)
    if reason:
        for s in list(book.positions):
            if s in available:book.sell(s,book.positions[s].qty,prices[s],ts,config,reason)
    return reason


def _monitor_open(book,marks,available,day,config):
    for s in list(book.positions):
        if s not in available:continue
        p=book.positions[s];mark=marks[s]
        if mark<=p.stop:book.sell(s,p.qty,mark,day,config,'OBSERVED_STOP')
        elif not p.took_profit and mark>=p.average_entry+config['profit_take_R']*(p.average_entry-p.initial_stop):
            book.sell(s,p.qty/2,mark,day,config,'OBSERVED_PARTIAL_TP')
            if s in book.positions:book.positions[s].took_profit=True
    return _observable_risk(book,marks,available,day,config)


def _annotate_fills(book,begin,campaign_ids):
    for row in book.fills[begin:]:
        s=row['symbol']
        if row['side']=='BUY':
            if row['reason']=='OPEN_PROBE' or s not in campaign_ids:
                campaign_ids[s]=row['ts_ms']
            row['opened_ms']=campaign_ids[s]
        # SELL rows already carry the position's opening identity. Do not
        # eagerly read a mapping that another sell in this batch can clear.
        row['campaign_id']=s+':'+str(row['opened_ms'])
    for s in list(campaign_ids):
        if s not in book.positions:campaign_ids.pop(s)


def campaign_metrics(fills,writeoffs,open_positions):
    groups={}
    for f in fills:
        key=f['campaign_id'];g=groups.setdefault(key,{'campaign_id':key,'symbol':f['symbol'],
            'opened_ms':f['opened_ms'],'entry_cost':0.,'proceeds':0.,'fees':0.,'pnl':0.,'buy_count':0,'sell_count':0,'exit_ms':None})
        g['fees']+=f['fee']
        if f['side']=='BUY':g['entry_cost']+=f['notional']+f['fee'];g['buy_count']+=1
        else:g['proceeds']+=f['notional']-f['fee'];g['pnl']+=f['realized_pnl'];g['sell_count']+=1;g['exit_ms']=f['ts_ms']
    for w in writeoffs:
        key=w['campaign_id'];g=groups[key];g['pnl']-=w['cost_basis_lost'];g['exit_ms']=w['ts_ms'];g['writeoff']=True
    open_ids={s+':'+str(p.opened_ms) for s,p in open_positions.items()}
    for key,g in groups.items():
        g['closed']=key not in open_ids
        g['return_on_deployed_cost']=g['pnl']/g['entry_cost'] if g['closed'] else None
        g['holding_days']=(g['exit_ms']-g['opened_ms'])/DAY if g['closed'] and g['exit_ms'] is not None else None
    closed=[g for g in groups.values() if g['closed']];wins=[g['pnl'] for g in closed if g['pnl']>0];losses=[-g['pnl'] for g in closed if g['pnl']<0]
    stats={'trade_count':len(closed),'open_campaign_count':len(open_ids),'winning_trades':len(wins),'losing_trades':len(losses),
           'win_rate':len(wins)/len(closed) if closed else None,
           'average_win':statistics.mean(wins) if wins else None,'average_loss':statistics.mean(losses) if losses else None,
           'payoff_ratio':statistics.mean(wins)/statistics.mean(losses) if wins and losses else None,
           'profit_factor':sum(wins)/sum(losses) if losses else None,
           'expectancy_per_closed_trade':statistics.mean(g['pnl'] for g in closed) if closed else None}
    return list(groups.values()),stats


def period_metrics(equity,initial,period):
    out={};previous=initial
    for row in equity:
        key=datetime.fromtimestamp(row['ts_ms']/1000,timezone.utc).strftime('%Y' if period=='year' else '%Y-%m')
        g=out.setdefault(key,{'initial_equity':previous,'final_equity':previous,'peak':previous,'max_drawdown':0.,'days':0,'exposure_sum':0.})
        g['peak']=max(g['peak'],row['equity']);g['max_drawdown']=min(g['max_drawdown'],row['equity']/g['peak']-1)
        g['final_equity']=row['equity'];g['days']+=1;g['exposure_sum']+=row.get('exposure_before_end_liquidation',row['exposure']);previous=row['equity']
    for g in out.values():
        g['return']=g['final_equity']/g['initial_equity']-1;g['average_exposure']=g.pop('exposure_sum')/g['days'];g.pop('peak')
    return out


def run(panel,config,start,end,missing='zero',liquidate_end=True,*,planner=None,signal_schedule=None):
    """Run a fixed-policy chronological account, without fitting on the test period."""
    validate_config(config)
    if missing not in {'zero','fail'}:raise ValueError('invalid_missing_policy')
    if start>=end:raise ValueError('invalid_test_period')
    panel.config=config;btc=panel.h['BTCUSDT'];test=[b for b in btc if start<=b.open_ms<end]
    if not test or test[0].open_ms!=start or test[-1].open_ms!=end-DAY:raise ValueError('test_period_missing_btc_boundary')
    if any(b.open_ms!=a.open_ms+DAY for a,b in zip(btc,btc[1:])):raise ValueError('BTC_calendar_gap')
    book=Book(config['initial_cash']);pending=[];equity=[];plans=[];losses=[];notes=Counter();prior={};campaign_ids={}
    benchmark_qty=config['initial_cash']/(test[0].open*(1+config['slippage_rate'])*(1+config['fee_rate']))
    for b in btc:
        day=b.open_ms
        if day<start-2*DAY:continue
        if day>=end:break
        # Presence of a daily row supplies an open proxy. Today's volume/close
        # never filters an opening order. Zero-volume opens remain an explicit
        # execution limitation, not a hindsight liquidity filter.
        bars=(panel.market_day(day) if hasattr(panel,'market_day') else
              {s:x for s in panel.h if (x:=panel.bar(s,day)) is not None})
        opens={s:x.open for s,x in bars.items()};closes={s:x.close for s,x in bars.items()}
        if day>=start:
            absent=set(book.positions)-set(bars)
            absent.update(s for s in book.positions if day in IDENTITY_BOUNDARIES.get(s,[]))
            marks={**prior,**opens}
            for s in absent:marks[s]=prior[s]
            available=set(opens)-absent
            book.begin_day(day,prior or opens);fbegin=len(book.fills)
            risk=_monitor_open(book,marks,available,day,config)
            for plan in list(pending):
                if plan['execute_after_ms']>day:continue
                if not risk:
                    executable=deepcopy(plan)
                    # Retain an unavailable held market without creating an
                    # imaginary sale. It remains in opening equity and risk.
                    unavailable=set(book.positions)-available
                    executable['targets']=[t for t in executable['targets'] if t['symbol'] in available]
                    eq=book.equity(marks)
                    for s in unavailable:
                        p=book.positions[s]
                        executable['targets'].append({'symbol':s,'target_weight':p.qty*marks[s]/eq,
                            'risk_fraction':0.,'stop':p.stop,'reference_price':marks[s],'allow_add':False,'stage':'PROBE'})
                    notes.update(book.apply_plan(executable,marks,day,config))
                pending.remove(plan)
            _annotate_fills(book,fbegin,campaign_ids)
            # A simultaneous-low portfolio bound ignores protective stops and
            # does not claim the lows occurred together or were executable.
            low_equity=book.cash+sum(p.qty*(bars[s].low if s in available else 0.) for s,p in book.positions.items())
            low_dd=low_equity/book.peak-1 if book.peak else 0.
            fbegin=len(book.fills)
            for s in sorted(absent):
                if s not in book.positions:continue
                if missing=='fail':raise ValueError('held_market_missing:'+s)
                opened=book.positions[s].opened_ms
                w=writeoff(book,s,b.close_ms);w.update(opened_ms=opened,campaign_id=s+':'+str(opened));losses.append(w);campaign_ids.pop(s,None)
            book.monitor_bar({s:x for s,x in bars.items() if s not in absent},b.close_ms,config)
            _annotate_fills(book,fbegin,campaign_ids)
            eq=book.equity(closes);book.peak=max(book.peak,eq)
            exposure=(eq-book.cash)/eq
            equity.append({'ts_ms':b.close_ms,'equity':eq,'cash':book.cash,'exposure':exposure,
                'open_risk_fraction':book.open_risk(closes,config)/eq,'btc_equity':benchmark_qty*b.close,
                'drawdown':eq/book.peak-1,'intraday_low_equity_bound':low_equity,'intraday_low_drawdown_bound':low_dd,
                'halted':book.halted,'position_count':len(book.positions)})
        scheduled=(signal_schedule(day) if signal_schedule else datetime.fromtimestamp(day/1000,timezone.utc).weekday()==6)
        if scheduled:
            plan=(planner or make_swing_plan)(panel,book,day,config,closes);plans.append(plan);pending.append(plan)
        prior=closes
    if liquidate_end:
        fbegin=len(book.fills);book.liquidate(prior,test[-1].close_ms,config,'END_OF_TEST_LIQUIDATION');_annotate_fills(book,fbegin,campaign_ids)
        equity[-1]['exposure_before_end_liquidation']=equity[-1]['exposure']
        equity[-1].update(equity=book.cash,cash=book.cash,exposure=0.,drawdown=book.cash/book.peak-1,open_risk_fraction=0.,position_count=0)
        equity[-1]['btc_equity']*=(1-config['slippage_rate'])*(1-config['fee_rate'])
    initial=config['initial_cash'];summary=metrics([r['equity'] for r in equity],initial)
    campaigns,trade_stats=campaign_metrics(book.fills,losses,book.positions)
    years=period_metrics(equity,initial,'year');months=period_metrics(equity,initial,'month')
    for key,g in years.items():
        trades=[t for t in campaigns if t['closed'] and datetime.fromtimestamp(t['exit_ms']/1000,timezone.utc).strftime('%Y')==key]
        w=[t['pnl'] for t in trades if t['pnl']>0];l=[-t['pnl'] for t in trades if t['pnl']<0]
        g.update(trade_count=len(trades),payoff_ratio=statistics.mean(w)/statistics.mean(l) if w and l else None,
                 profit_factor=sum(w)/sum(l) if l else None)
    if liquidate_end and abs(book.realized_pnl-(book.cash-initial))>1e-6:raise AssertionError('PnL_reconciliation')
    summary.update(trade_stats)
    states=Counter(d['state'] for p in plans for d in p['decisions'])
    rejections=Counter(reason for p in plans for d in p['decisions'] if d['state']=='REJECT' for reason in d['reasons'])
    funnel={'weekly_universe_symbol_dates':sum(len(p['universe']) for p in plans),
            'candidate_signal_count':states['ENTER']+states['WAIT_CAPACITY'],
            'capacity_rejections':states['WAIT_CAPACITY'],'states':dict(states),'entry_rejection_reasons':dict(rejections)}
    summary.update(start=iso(start),end_exclusive=iso(end),final_equity=equity[-1]['equity'],
        cagr=(equity[-1]['equity']/initial)**(365.25/len(equity))-1,
        fees_paid=book.total_fees,slippage_cost=sum(f['quantity']*abs(f['fill_price']-f['reference_price']) for f in book.fills),
        funding_paid=0.,funding_basis='UNBORROWED_SPOT_NOT_PERPETUAL',
        fill_count=len(book.fills),buy_fill_count=sum(f['side']=='BUY' for f in book.fills),
        average_daily_exposure=statistics.mean(r.get('exposure_before_end_liquidation',r['exposure']) for r in equity),
        average_daily_open_risk=statistics.mean(r['open_risk_fraction'] for r in equity),
        intraday_low_drawdown_bound=min([0.]+[r['intraday_low_drawdown_bound'] for r in equity]),
        latched_drawdown_halt=book.halted,writeoff_count=len(losses),writeoff_cost_basis=sum(w['cost_basis_lost'] for w in losses),
        monthly_median_return=statistics.median(g['return'] for g in months.values()),
        positive_month_fraction=sum(g['return']>0 for g in months.values())/len(months),
        btc=metrics([r['btc_equity'] for r in equity],initial),execution_notes=dict(notes),
        signal_plans=len(plans),signal_funnel=funnel,liquidated_at_end=liquidate_end,
        turnover_notional=sum(f['notional'] for f in book.fills))
    return {'summary':summary,'yearly':years,'monthly':months,'equity':equity,'fills':book.fills,
            'writeoffs':losses,'campaigns':campaigns,'plans':plans,'config':deepcopy(config),
            'open_positions':book.to_dict()['positions']}
