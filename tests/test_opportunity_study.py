import copy
import json
import math
import unittest
from pathlib import Path
from test_ai_native import packet
from one_trading.ai_native.engine import instant, Protocol
from one_trading.ai_native.causal_features import build
from one_trading.ai_native.opportunity_study import feature_grid, simulate, signal, analyze


def protocol():
    return json.loads(Path('configs/opportunity_v1.json').read_text())


def tape(frames):
    times=[instant(f['received_at']).timestamp() for f in frames]
    return dict(frames=frames,times=times,source_times=[{s:t for s in f['quotes']} for f,t in zip(frames,times)])


class Opportunity(unittest.TestCase):
    def test_bounded_features_match_full_prefix_and_future_is_unused(self):
        f=[packet(i,100+i*.001,second_step=1) for i in range(1000)]
        grid=feature_grid(tape(f),protocol())
        for g in grid:
            expected=build(f,f[g['index']]['received_at'],vars(Protocol()))
            self.assertEqual(g['features']['symbols'],expected['symbols'])
        altered=copy.deepcopy(f)
        for x in altered[601:]:x['quotes']['BTCUSDT']['bid']=9999;x['quotes']['BTCUSDT']['ask']=10000
        self.assertEqual(grid[:2],feature_grid(tape(altered),protocol())[:2])

    def test_delay_cost_compound_and_first_touch(self):
        f=[packet(i,100 if i<10 else 102,second_step=1) for i in range(50)]
        r=simulate(tape(f),protocol(),0,'BTCUSDT',30,2,.004)
        self.assertEqual((r['entry_index'],r['exit_index'],r['touch']),(3,13,'TARGET'))
        cost=11*1.001;proceeds=11/(100.1*1.0005)*102*.9995*.999
        self.assertAlmostEqual(r['pnl'],proceeds-cost)
        self.assertAlmostEqual(r['log_growth'],math.log1p((proceeds-cost)/10000))

    def test_stop_before_target_and_tail(self):
        f=[packet(i,100 if i<10 else 98 if i<20 else 110,second_step=1) for i in range(50)]
        r=simulate(tape(f),protocol(),0,'BTCUSDT',30,2,.004)
        self.assertEqual(r['touch'],'STOP');self.assertEqual(r['exit_index'],13)
        self.assertEqual(simulate(tape(f),protocol(),30,'BTCUSDT',30,2,.004)['status'],'RIGHT_CENSORED')

    def test_depth_staleness_and_original_receipt(self):
        for kind in ('depth','old','stale','spread'):
            f=[packet(i,second_step=1) for i in range(50)];t=tape(f)
            if kind=='depth':f[3]['quotes']['BTCUSDT']['ask_size']=.001
            if kind=='old':t['source_times'][3]['BTCUSDT']=t['times'][0]
            if kind=='stale':f[3]['quotes']['BTCUSDT']['exchange_at']=packet(0,age=50)['quotes']['BTCUSDT']['exchange_at']
            if kind=='spread':f[3]['quotes']['BTCUSDT']['ask']=110
            self.assertIn('REJECTED',simulate(t,protocol(),0,'BTCUSDT',30,2,.004)['status'])

    def test_unfilled_exit_is_mark_not_fill_and_blocks(self):
        f=[packet(i,second_step=1,bid_depth=.001) for i in range(50)]
        r=simulate(tape(f),protocol(),0,'BTCUSDT',30,2,.004)
        self.assertEqual(r['status'],'OPEN_UNLIQUIDATED')
        self.assertEqual(r['mark_or_fill'],'FINAL_BID_MARK_NOT_FILL')

    def test_gap_and_prior_peak_drawdown(self):
        f=[packet(i,second_step=1) for i in range(50) if i not in (15,16,17)]
        r=simulate(tape(f),protocol(),0,'BTCUSDT',30,2,.004,prior_peak=11000)
        self.assertTrue(r['gap_uncertain']);self.assertGreater(r['intratrade_drawdown'],.09)

    def test_cash_no_trades_and_partition_no_future(self):
        f=[packet(i,100+i*.01,second_step=1) for i in range(1600)]
        p=protocol();p.update(horizons_seconds=[300],net_reward_risk=[2],symbols=['BTCUSDT'])
        t=tape(f);r=analyze(t,p,feature_grid(t,p))
        for s in r['strategies']:
            if s['strategy']=='CASH':self.assertEqual(s['pnl'],0);self.assertEqual(s['trades'],[])
            if s['partition']=='DISCOVERY':
                for tr in s['trades']:
                    if tr['exit_index'] is not None:self.assertLess(t['times'][tr['exit_index']],(t['times'][0]+t['times'][-1])/2)

