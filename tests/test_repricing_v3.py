"""Synthetic tests of v3 boundaries, not investment-performance evidence."""
from collections import defaultdict
from copy import deepcopy
import unittest
from unittest.mock import patch

from one_trading.historical_data import ms
from one_trading.observer import Bar, DAY
from one_trading.portfolio import Book, Position
from one_trading.repricing_v3 import AtlasPanel, STRATEGIES, config_for, flags, mature_in_era, make_plan, run
from one_trading.repricing_atlas import labels, path_labels, rate_statistics
from test_cashflow_alpha import histories


class LabelPanel:
    def __init__(self, bars):
        self.h = {'AAAUSDT':bars}
        self.rows = {b.open_ms:b for b in bars}
    def bar(self, symbol, day):
        return self.rows.get(day)


def bar(day, o=100., h=110., l=90., c=100.):
    return Bar(day, day+DAY-1, o, h, l, c, 100., 2e6)


class RepricingTests(unittest.TestCase):
    def test_strict_maturity_boundary(self):
        end = ms('2026-10-01')
        self.assertFalse(mature_in_era(end-181*DAY))
        self.assertTrue(mature_in_era(end-182*DAY))
        self.assertFalse(mature_in_era(ms('2022-12-01')))

    def test_labels_stop_first_and_mae_include_target_bar(self):
        d=ms('2020-01-01')
        p=LabelPanel([bar(d+i*DAY, h=210. if i==2 else 110., l=80. if i==2 else 90.) for i in range(182)])
        r=labels(p,'AAAUSDT',d)
        self.assertTrue(r['research']['90d_2x']['hit'])
        self.assertEqual(r['research']['90d_2x']['days'],2)
        self.assertAlmostEqual(r['research']['90d_2x']['mae'],-.2)
        self.assertTrue(r['delayed']['180d_2x']['stop_before_or_same_bar'])
        self.assertFalse(r['research']['180d_3x']['hit'])

    def test_target_after_gap_not_counted_but_prior_hit_retained(self):
        d=ms('2020-01-01')
        p=LabelPanel([bar(d+i*DAY,h=210. if i==2 else 400. if i==5 else 110.) for i in range(182) if i!=4])
        r=labels(p,'AAAUSDT',d)['research']
        self.assertTrue(r['180d_2x']['hit'])
        self.assertFalse(r['180d_3x']['hit'])
        self.assertEqual(r['180d_end_return'],-1.)

    def test_no_entry_retained_as_failure(self):
        d=ms('2020-01-01')
        p=LabelPanel([bar(d+i*DAY,h=400.) for i in range(182) if i!=2])
        r=labels(p,'AAAUSDT',d)
        self.assertFalse(r['entry_available'])
        self.assertFalse(r['delayed']['180d_2x']['hit'])
        self.assertEqual(r['delayed']['180d_end_return'],-1.)

    def test_horizon_day_90_and_180_are_inclusive(self):
        d=ms('2020-01-01')
        p=LabelPanel([bar(d+i*DAY,h=200. if i==90 else 300. if i==180 else 110.) for i in range(182)])
        r=labels(p,'AAAUSDT',d)['research']
        self.assertEqual(r['90d_2x']['days'],90)
        self.assertFalse(r['90d_3x']['hit'])
        self.assertEqual(r['180d_3x']['days'],180)

    def test_flags_distinct_strict_and_inclusive(self):
        r=dict(btc_above=True,close=101.,sma50=100.,r28=.30,r7=.15,volume_ratio=1.5,
               relative28=.01,tr_ratio=.70,prior20_high=100.,relative90=.01)
        self.assertTrue(all(flags(r).values()))
        r['relative28']=0.
        self.assertFalse(flags(r)['demand'])
        self.assertTrue(flags(r)['compression'])
        r['prior20_high']=r['close']
        self.assertFalse(flags(r)['compression'])

    def test_episode_cooldown_before_capacity(self):
        h=histories();c=config_for('price_control')
        with patch('one_trading.repricing_v3.flags', return_value={s:True for s in STRATEGIES}):
            p=AtlasPanel(h,c).prepare()
        dates=[d for d,syms in p.episodes['price_control'].items() if 'AAAUSDT' in syms]
        self.assertTrue(dates)
        self.assertTrue(all(b-a>=182*DAY for a,b in zip(dates,dates[1:])))

    def test_no_future_listing_or_gap_survivorship_filter(self):
        h=histories();d=ms('2019-01-06');c=config_for('price_control')
        h['FUTUREUSDT']=[b for b in h['AAAUSDT'] if b.open_ms>d]
        h['ENDEDUSDT']=[b for b in h['AAAUSDT'] if b.open_ms<=d]
        p=AtlasPanel(h,c)
        self.assertNotIn('FUTUREUSDT',p.universe(d))
        self.assertIn('ENDEDUSDT',p.universe(d))
        h['ENDEDUSDT']=[b for b in h['ENDEDUSDT'] if b.open_ms!=d-DAY]
        self.assertNotIn('ENDEDUSDT',AtlasPanel(h,c).universe(d))

    def test_delayed_entry_costs_no_adds_and_prefix_nonvacuous(self):
        h=histories();c=config_for('price_control',dict(fee_rate=.002,slippage_rate=.003))
        start=ms('2019-01-01');cut=start+50*DAY
        with patch('one_trading.repricing_v3.flags',return_value={s:True for s in STRATEGIES}):
            p=AtlasPanel(h,c).prepare()
            full=run(p,c,start,cut,liquidate_end=False)
            truncated={s:[b for b in bs if b.open_ms<cut] for s,bs in h.items()}
            short=run(AtlasPanel(truncated,c).prepare(),c,start,cut,liquidate_end=False)
        self.assertEqual(full,short)
        buys=[f for f in full['fills'] if f['side']=='BUY']
        self.assertTrue(buys)
        self.assertTrue(all(f['reason']=='OPEN_PROBE' for f in buys))
        for f in buys:
            self.assertTrue(any(p['signal_day_ms']+2*DAY==f['ts_ms'] for p in full['plans']))

    def test_inference_false_positive_denominators(self):
        rows=[]
        for i,(signal,success) in enumerate([(True,True),(True,False),(False,False),(False,True)]):
            rows.append(dict(symbol=str(i),day=ms(f'{2020+i}-01-01'),volume_ratio=2 if signal else 1,
                relative28=0,tr_ratio=1,btc_above=True,**{s:signal for s in STRATEGIES},
                research={'180d_2x':{'hit':success}}))
        r=rate_statistics(rows,'demand','180d_2x',repetitions=50)
        self.assertEqual(r['base_probability'],.5)
        self.assertEqual(r['false_positive_rate'],.5)
        self.assertEqual(r['false_discovery_rate'],.5)

    def test_held_position_cannot_add(self):
        h=histories();d=ms('2019-01-06');c=config_for('demand');p=AtlasPanel(h,c).prepare()
        price=p.bar('AAAUSDT',d).close
        b=Book(9000.)
        b.positions['AAAUSDT']=Position('AAAUSDT',5,5*price,d-DAY,.85*price,.85*price,price)
        plan=make_plan(p,b,d,c,{'AAAUSDT':price})
        self.assertTrue(plan['targets'])
        self.assertFalse(plan['targets'][0]['allow_add'])

    def test_independent_path_agrees_in_gap_and_same_bar_cases(self):
        from scripts.validate_repricing_v3 import IndependentSource, eq_tree
        d=ms('2020-01-01')
        bs=[bar(d+i*DAY,h=210. if i==3 else 310. if i==95 else 110.,
                l=80. if i==3 else 90.) for i in range(182) if i!=100]
        independent=IndependentSource({'AAAUSDT':bs})
        expected=independent.path('AAAUSDT',d+DAY,100.,85.)
        actual=path_labels(LabelPanel(bs),'AAAUSDT',d+DAY,100.,85.)
        eq_tree(expected,actual)
        actual['180d_3x']['hit']=False
        with self.assertRaises(AssertionError):
            eq_tree(expected,actual)


if __name__ == '__main__':
    unittest.main()
