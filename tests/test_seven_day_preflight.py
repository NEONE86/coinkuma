import copy
import json
import unittest
from pathlib import Path
from one_trading.ai_native.seven_day_preflight import frozen_contract,assess,counterfactual_depth


class SevenDayPreparation(unittest.TestCase):
    def test_freezes_original_and_only_selects_requested_arms(self):
        p=json.loads(Path('configs/opportunity_v1.json').read_bytes());before=copy.deepcopy(p)
        c=frozen_contract(p)
        self.assertEqual(p,before);self.assertEqual(c['base_protocol'],p)
        self.assertEqual(c['selected_strategies'],['REVERSION','TREND','CASH'])
        self.assertEqual((c['selected_horizon_seconds'],c['selected_net_reward_risk']),(14400,2))
        self.assertIsNone(c['deadline_at']);self.assertEqual(c['activation_status'],'NOT_ACTIVATED')
        p['position_fraction']=.01
        with self.assertRaisesRegex(ValueError,'CHANGED'):frozen_contract(p)

    def test_storage_boundary_cannot_authorize_collection(self):
        self.assertFalse(assess(28_999_999_999)['storage_ok'])
        r=assess(29_000_000_000)
        self.assertTrue(r['storage_ok']);self.assertFalse(r['collection_started'])
        self.assertEqual(r['status'],'BLOCKED_NOT_STARTED')
        self.assertEqual(r['expected_raw_bytes'],9_911_150_935)
        for v in (-1,True,1.0):
            with self.assertRaises(ValueError):assess(v)

    def test_capacity_is_depth_only_and_does_not_change_pnl(self):
        q=dict(bid=100,ask=100,ask_size=200,bid_size=1000)
        rows=counterfactual_depth(q)
        self.assertEqual([r['entry_l1_size_possible'] for r in rows],[True,False,False])
        self.assertTrue(all('pnl' not in r for r in rows))
        self.assertTrue(all(r['classification']=='COUNTERFACTUAL_SIZE_ONLY_NOT_EXECUTION_OR_RETURN' for r in rows))
        for bad in (dict(q,ask=0),dict(q,bid_size=float('nan'))):
            with self.assertRaises(ValueError):counterfactual_depth(bad)
