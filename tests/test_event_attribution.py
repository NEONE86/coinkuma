import unittest
from one_trading.event_attribution import path,next_designs,H

class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.costs=dict(capital_usdt=10000,exposure_fraction=.125,fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1)
        self.bars={'BTCUSDT':{n*H:[n*H,'100','103','98','100'] for n in range(-2,25)}}
    def test_flat_price_loses_roundtrip_cost_every_fixed_horizon(self):
        p=path(self.bars,'BTCUSDT',0,self.costs)
        for m in p['markouts']:
            self.assertAlmostEqual(m['net_usdt'],-2500*.0016/1.0016)
            self.assertAlmostEqual(m['mae'],-.02);self.assertAlmostEqual(m['mfe'],.03)
            self.assertEqual(m['price_return'],0)
    def test_no_trade_selector_stays_cash(self):
        p=path(self.bars,None,0,self.costs)
        self.assertEqual(p['cash_deployed_usdt'],0);self.assertIsNone(p['entry_price'])
        self.assertTrue(all(m['net_usdt']==m['trading_cost_usdt']==0 for m in p['markouts']))
    def test_missing_horizon_fails_no_imputation(self):
        del self.bars['BTCUSDT'][4*H]
        with self.assertRaises(KeyError):path(self.bars,'BTCUSDT',0,self.costs)
    def test_future_designs_frozen_and_not_auto_enabled(self):
        ds=next_designs({},'2026-10-29T15:17:23+00:00','a'*64)
        self.assertEqual(len(ds),2)
        for d in ds:
            self.assertEqual(d['start_signal_at'],'2026-10-29T16:00:00+00:00')
            self.assertEqual(d['earliest_full_label_due'],'2026-10-30T18:02:00+00:00')
            self.assertEqual(d['origin'],'POST_HOC_GENERATED');self.assertFalse(d['auto_execution']);self.assertEqual(d['model_calls'],0)

if __name__=='__main__':unittest.main()
