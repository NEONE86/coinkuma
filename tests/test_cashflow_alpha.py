"""Software regressions; fabricated bars are not investment-performance evidence."""
from copy import deepcopy
import math
import unittest
from unittest.mock import patch
from one_trading.cashflow_alpha import (default_config, run, signal_row, make_swing_plan,
    period_metrics, _annotate_fills, _monitor_open, campaign_metrics)
from one_trading.historical_data import ms
from one_trading.observer import Bar, DAY
from one_trading.portfolio import Book, Position
from one_trading.walkforward import HistoricalPanel


def histories(n=520):
    start=ms('2018-01-01');out={'BTCUSDT':[],'AAAUSDT':[],'BBBUSDT':[]}
    for i in range(n):
        btc=100.+.015*i
        a=130. if i<260 else 100.+max(0,i-330)*.15+.2*math.sin(i/7.)
        for s,p,q in [('BTCUSDT',btc,1e8),('AAAUSDT',a,2e6),('BBBUSDT',a*.7,1.5e6)]:
            out[s].append(Bar(start+i*DAY,start+(i+1)*DAY-1,p,p*1.025,p*.975,p,10000.,q))
    return out


class CashflowTests(unittest.TestCase):
    def setUp(self):
        self.c=default_config();self.h=histories();self.start=ms('2019-01-01');self.end=self.start+90*DAY
    def panel(self,h=None):return HistoricalPanel(h or self.h,self.c)
    def test_actual_fixture_generates_nonvacuous_delayed_trades(self):
        result=run(self.panel(),self.c,self.start,self.end)
        buys=[f for f in result['fills'] if f['side']=='BUY']
        self.assertTrue(buys)
        for f in buys:
            eligible=[p for p in result['plans'] if p['execute_after_ms']==f['ts_ms']]
            self.assertTrue(eligible)
            self.assertTrue(any(p['signal_day_ms']+2*DAY==f['ts_ms'] for p in eligible))
        self.assertAlmostEqual(result['summary']['funding_paid'],0.)
        self.assertEqual(result['summary']['signal_funnel']['candidate_signal_count'],
                         sum(d['state'] in {'ENTER','WAIT_CAPACITY'} for p in result['plans'] for d in p['decisions']))
    def test_future_mutation_does_not_change_prior_run(self):
        first=run(self.panel(),self.c,self.start,self.end,liquidate_end=False)
        self.assertGreater(first['summary']['buy_fill_count'],0)
        h=deepcopy(self.h)
        for s,bs in h.items():
            for i,b in enumerate(bs):
                if b.open_ms>=self.end:bs[i]=Bar(b.open_ms,b.close_ms,1e6,2e6,1.,1e6,1e15,1e20)
        second=run(self.panel(h),self.c,self.start,self.end,liquidate_end=False)
        self.assertEqual(first,second)
    def test_prefix_truncation_does_not_change_prior_run(self):
        full=run(self.panel(),self.c,self.start,self.end,liquidate_end=False)
        short={s:[b for b in bs if b.open_ms<self.end] for s,bs in self.h.items()}
        self.assertEqual(full,run(self.panel(short),self.c,self.start,self.end,liquidate_end=False))
    def test_costs_campaigns_and_terminal_cash_reconcile(self):
        r=run(self.panel(),self.c,self.start,self.end)
        self.assertTrue(all(t['closed'] for t in r['campaigns']))
        self.assertAlmostEqual(sum(t['pnl'] for t in r['campaigns']),r['summary']['final_equity']-self.c['initial_cash'])
        self.assertAlmostEqual(sum(f['fee'] for f in r['fills']),r['summary']['fees_paid'])
        self.assertEqual(r['equity'][-1]['exposure'],0.)
        self.assertIn('exposure_before_end_liquidation',r['equity'][-1])
        self.assertEqual(r['equity'][-1]['cash'],r['equity'][-1]['equity'])
    def test_missing_held_market_is_a_loss_not_recovery_fill(self):
        r=run(self.panel(),self.c,self.start,self.end)
        first=next(f for f in r['fills'] if f['side']=='BUY');gap=first['ts_ms']+DAY
        h=deepcopy(self.h);h[first['symbol']]=[b for b in h[first['symbol']] if b.open_ms!=gap]
        r=run(self.panel(h),self.c,self.start,self.end)
        self.assertGreater(r['summary']['writeoff_count'],0)
        self.assertTrue(all(w['proceeds']==0 for w in r['writeoffs']))
        self.assertAlmostEqual(sum(t['pnl'] for t in r['campaigns']),r['summary']['final_equity']-self.c['initial_cash'])
        with self.assertRaisesRegex(ValueError,'held_market_missing'):
            run(self.panel(h),self.c,self.start,self.end,missing='fail')
    def test_weekly_signal_never_uses_incomplete_calendar_period(self):
        sunday=ms('2019-01-06');row=signal_row(self.panel(),'AAAUSDT',sunday)
        self.assertLess(row['max_input_close_ms'],sunday+DAY)
        plan=make_swing_plan(self.panel(),Book(10000.),sunday,self.c,{'AAAUSDT':row['metrics']['last_close']})
        self.assertEqual(plan['observed_ms'],sunday+DAY)
        self.assertEqual(plan['execute_after_ms'],sunday+2*DAY)
        with self.assertRaisesRegex(ValueError,'completed_sunday'):
            make_swing_plan(self.panel(),Book(10000.),sunday+DAY,self.c,{})
    def test_historical_liquidity_not_current_winner_list(self):
        h=deepcopy(self.h);sunday=ms('2019-01-06');h['FUTUREUSDT']=h['AAAUSDT'][-30:]
        p=self.panel(h);plan=make_swing_plan(p,Book(10000.),sunday,self.c,{})
        self.assertNotIn('FUTUREUSDT',plan['universe'])
    def test_multiple_sells_same_batch_keep_position_identity(self):
        b=Book(900.);b.positions['A']=Position('A',1.,100.,0,90.,90.,100.)
        b.sell('A',.5,140.,DAY,self.c,'PARTIAL')
        b.sell('A',.5,140.,DAY,self.c,'LIQUIDATE')
        ids={'A':0};_annotate_fills(b,0,ids)
        self.assertEqual([r['campaign_id'] for r in b.fills],['A:0','A:0'])
        self.assertFalse(ids)
    def test_unobservable_position_does_not_suppress_other_open_stop(self):
        b=Book(800.);b.positions['MISSING']=Position('MISSING',1.,100.,0,90.,90.,100.)
        b.positions['VISIBLE']=Position('VISIBLE',1.,100.,0,90.,90.,100.)
        b.begin_day(DAY,{'MISSING':100.,'VISIBLE':100.})
        _monitor_open(b,{'MISSING':100.,'VISIBLE':85.},{'VISIBLE'},DAY,self.c)
        self.assertIn('MISSING',b.positions);self.assertNotIn('VISIBLE',b.positions)
        self.assertEqual(b.fills[0]['reference_price'],85.)
    def test_add_confirmation_uses_original_entry_r_not_average_after_add(self):
        sunday=ms('2019-01-20');opened=sunday-21*DAY
        book=Book(9000.);book.positions['AAAUSDT']=Position('AAAUSDT',9.,990.,opened,100.,90.,110.)
        book.fills=[{'symbol':'AAAUSDT','side':'BUY','ts_ms':opened,'fill_price':100.}]
        row=deepcopy(signal_row(self.panel(),'AAAUSDT',sunday))
        row['metrics'].update(last_close=112.,sma50=100.,btc_risk_on=True,return_7d=.03,
                              return_28d=.08,extension_sma20=.04)
        with patch('one_trading.cashflow_alpha.signal_row',side_effect=lambda p,s,d: row if s=='AAAUSDT' else None):
            plan=make_swing_plan(self.panel(),book,sunday,self.c,{'AAAUSDT':112.})
        target=next(t for t in plan['targets'] if t['symbol']=='AAAUSDT')
        self.assertTrue(target['allow_add'])  # original 100 + (100 - 90), not average 110 + (110 - 90)
        decision=next(d for d in plan['decisions'] if d['symbol']=='AAAUSDT')
        self.assertEqual(decision['original_risk_per_unit'],10.)

    def test_metrics_include_initial_capital_and_calendar_boundary(self):
        rows=[{'ts_ms':ms('2020-12-31')+DAY-1,'equity':90.,'exposure':.1},
              {'ts_ms':ms('2021-01-01')+DAY-1,'equity':99.,'exposure':.2}]
        result=period_metrics(rows,100.,'year')
        self.assertAlmostEqual(result['2020']['max_drawdown'],-.1)
        self.assertAlmostEqual(result['2021']['return'],.1)
    def test_campaign_groups_partial_exit_and_add_as_one_trade(self):
        fills=[{'campaign_id':'A:0','symbol':'A','opened_ms':0,'ts_ms':0,'side':'BUY','notional':100.,'fee':1.,'realized_pnl':None},
               {'campaign_id':'A:0','symbol':'A','opened_ms':0,'ts_ms':DAY,'side':'BUY','notional':50.,'fee':.5,'realized_pnl':None},
               {'campaign_id':'A:0','symbol':'A','opened_ms':0,'ts_ms':2*DAY,'side':'SELL','notional':80.,'fee':.8,'realized_pnl':3.45},
               {'campaign_id':'A:0','symbol':'A','opened_ms':0,'ts_ms':3*DAY,'side':'SELL','notional':90.,'fee':.9,'realized_pnl':13.35}]
        groups,stats=campaign_metrics(fills,[],{})
        self.assertEqual(stats['trade_count'],1);self.assertEqual(groups[0]['buy_count'],2)
        self.assertAlmostEqual(groups[0]['pnl'],16.8)

if __name__=='__main__':unittest.main()
