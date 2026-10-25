"""Synthetic behavior tests, never market-return evidence."""
from copy import deepcopy
import statistics
import unittest
from unittest.mock import patch

from one_trading.cashflow_alpha_v2 import (A, B, ChallengerPanel, default_config,
    signal_row, entry_reasons, make_plan, run)
from one_trading.historical_data import ms
from one_trading.observer import DAY, Bar
from one_trading.portfolio import Book, Position
from test_cashflow_alpha import histories


def passing_metrics():
    return dict(btc_risk_on=True, stop_distance=.10, prior28_below_sma50_count=14,
                last7_crossed_above_sma50=True,last_close=100.,sma50=98.,
                drawdown_180d=.10,relative_return_28d=.03,return_7d=.08,
                return_28d=.10,tr_contraction=.90,completed_week_up=True,
                sma200=90.,sma50_20days_ago=97.,drawdown_60d=.08,
                close_to_sma20=.95,previous_close=99.,daily_open=99.,relative_return_90d=.01)


class ChallengerTests(unittest.TestCase):
    def test_exact_inclusive_and_strict_boundaries(self):
        for strategy in (A,B):
            m=passing_metrics()
            self.assertEqual(entry_reasons({'metrics':m},strategy),[])
            m['stop_distance']=.180001
            self.assertIn('STOP_DISTANCE',entry_reasons({'metrics':m},strategy))
        m=passing_metrics();m['relative_return_28d']=.029999
        self.assertIn('RELATIVE_28D',entry_reasons({'metrics':m},A))
        m=passing_metrics();m['relative_return_90d']=0.
        self.assertIn('RELATIVE_90D',entry_reasons({'metrics':m},B))
        m=passing_metrics();m['sma50_20days_ago']=m['sma50']
        self.assertIn('TREND_NOT_ESTABLISHED',entry_reasons({'metrics':m},B))

    def test_below_sma_window_and_atr_are_causal(self):
        h=histories();day=ms('2019-01-06');c=default_config(A)
        p=ChallengerPanel(h,c);r=signal_row(p,'AAAUSDT',day,A)
        i=p.index['AAAUSDT'][day];bs=h['AAAUSDT'][i-239:i+1];cs=[b.close for b in bs]
        n=sum(cs[j]<statistics.mean(cs[j-49:j+1]) for j in range(205,233))
        self.assertEqual(n,r['metrics']['prior28_below_sma50_count'])
        tr=[max(bs[j].high-bs[j].low,abs(bs[j].high-cs[j-1]),abs(bs[j].low-cs[j-1])) for j in range(226,240)]
        self.assertAlmostEqual(r['initial_stop'],cs[-1]-2.5*statistics.mean(tr))
        self.assertLess(r['max_input_close_ms'],day+DAY)

    def test_future_new_listing_and_gap_not_eligible(self):
        h=histories();d=ms('2019-01-06');c=default_config(B)
        h['FUTUREUSDT']=[b for b in h['AAAUSDT'] if b.open_ms>d]
        h['AAAUSDT']=[b for b in h['AAAUSDT'] if b.open_ms!=d-DAY]
        p=ChallengerPanel(h,c)
        self.assertNotIn('FUTUREUSDT',p.universe(d))
        self.assertNotIn('AAAUSDT',p.universe(d))

    def test_held_positions_never_add(self):
        for strategy in (A,B):
            c=default_config(strategy);h=histories();d=ms('2019-01-06')
            p=ChallengerPanel(h,c);b=Book(9000.)
            row=signal_row(p,'AAAUSDT',d,strategy);price=row['metrics']['last_close']
            b.positions['AAAUSDT']=Position('AAAUSDT',5.,5*price,d-2*DAY,price*.85,price*.85,price)
            plan=make_plan(p,b,d,c,{'AAAUSDT':price})
            for t in plan['targets']:
                if t['symbol']=='AAAUSDT':self.assertFalse(t['allow_add'])

    def test_cadence_and_delay_with_nonvacuous_fills(self):
        for strategy in (A,B):
            c=default_config(strategy);h=histories();start=ms('2019-01-01');end=start+40*DAY
            with patch('one_trading.cashflow_alpha_v2.entry_reasons',return_value=[]):
                r=run(ChallengerPanel(h,c),c,start,end)
            buys=[f for f in r['fills'] if f['side']=='BUY']
            self.assertTrue(buys)
            self.assertTrue(all(f['reason']=='OPEN_PROBE' for f in buys))
            for f in buys:
                self.assertTrue(any(p['execute_after_ms']==f['ts_ms'] for p in r['plans']))
            self.assertTrue(all(p['execute_after_ms']==p['signal_day_ms']+2*DAY for p in r['plans']))
            if strategy==A:self.assertTrue(all((p['signal_day_ms']//DAY+3)%7==6 for p in r['plans']))
            else:self.assertEqual(len(r['plans']),42)

    def test_prefix_invariance_with_nonvacuous_fills(self):
        for strategy in (A,B):
            c=default_config(strategy);h=histories();start=ms('2019-01-01');end=start+50*DAY
            truncated={s:[b for b in bs if b.open_ms<end] for s,bs in h.items()}
            with patch('one_trading.cashflow_alpha_v2.entry_reasons',return_value=[]):
                full=run(ChallengerPanel(h,c),c,start,end,liquidate_end=False)
                short=run(ChallengerPanel(truncated,c),c,start,end,liquidate_end=False)
            self.assertGreater(full['summary']['buy_fill_count'],0)
            self.assertEqual(full,short)

    def test_controls_are_deterministic_and_only_use_historical_pool(self):
        h=histories();c=default_config(B);d=ms('2019-01-06');p=ChallengerPanel(h,c)
        with patch('one_trading.cashflow_alpha_v2.entry_reasons',return_value=[]):
            first=make_plan(p,Book(10000.),d,c,{},control_seed=7)
            second=make_plan(p,Book(10000.),d,c,{},control_seed=7)
        self.assertEqual(first,second)
        self.assertTrue(set(t['symbol'] for t in first['targets'])<=set(first['universe']))

    def test_costs_and_no_adds_reconcile(self):
        c={**default_config(B),'fee_rate':.002,'slippage_rate':.003}
        start=ms('2019-01-01')
        with patch('one_trading.cashflow_alpha_v2.entry_reasons',return_value=[]):
            r=run(ChallengerPanel(histories(),c),c,start,start+40*DAY)
        self.assertAlmostEqual(sum(x['pnl'] for x in r['campaigns']),r['summary']['final_equity']-c['initial_cash'])
        self.assertEqual(r['summary']['funding_paid'],0.)
        self.assertTrue(all(x['buy_count']==1 for x in r['campaigns']))


if __name__=='__main__':unittest.main()
