from copy import deepcopy
import unittest
from unittest.mock import patch
from one_trading.cashflow_alpha_v2 import B,ChallengerPanel,default_config,run
from one_trading.historical_data import ms
from one_trading.observer import DAY
from scripts.validate_cashflow_v2 import daily_ledger,chronology,decision_for
from test_cashflow_alpha import histories


class V2AuditTests(unittest.TestCase):
    def fixture(self):
        h=histories();c=default_config(B);a=ms('2019-01-01')
        with patch('one_trading.cashflow_alpha_v2.entry_reasons',return_value=[]):
            r=run(ChallengerPanel(h,c),c,a,a+40*DAY)
        return h,r

    def test_daily_equity_tamper_detected_even_if_final_unchanged(self):
        h,r=self.fixture();daily_ledger(r,h)
        r['equity'][10]['equity']+=1.
        with self.assertRaisesRegex(AssertionError,'daily_source_marked_equity'):daily_ledger(r,h)

    def test_fee_tamper_and_early_buy_detected(self):
        h,r=self.fixture();chronology(r)
        modified=deepcopy(r);modified['fills'][0]['fee']+=1.
        with self.assertRaisesRegex(AssertionError,'fee_rate'):daily_ledger(modified,h)
        buy=next(f for f in r['fills'] if f['side']=='BUY');buy['ts_ms']-=DAY
        buy['reason']='ADD_CONFIRMED'
        with self.assertRaisesRegex(AssertionError,'adds_forbidden'):chronology(r)

    def test_decision_is_strict_and_missing_evidence_cannot_pass(self):
        row=dict(profit_factor=1.31,trade_count=250,expectancy_per_closed_trade=1.,
                 total_return=.1,max_drawdown=-.10,latched_drawdown_halt=False)
        comparison={'stress':{k:dict(row) for k in ['continuous','challenge']+[str(y) for y in range(2019,2027)]}}
        months={str(i):.02 for i in range(24)};boot={'monthly_mean_95pct_interval':[.01,.03]}
        controls=[{'mean_monthly_net_return':0.} for i in range(200)]
        self.assertEqual(decision_for(comparison,months,boot,controls,True)['decision'],'PASS')
        comparison['stress']['continuous']['profit_factor']=1.30
        self.assertEqual(decision_for(comparison,months,boot,controls,True)['decision'],'REJECT')
        self.assertEqual(decision_for(comparison,months,boot,controls,False)['decision'],'NEEDS_MORE_EVIDENCE')


if __name__=='__main__':unittest.main()
