"""Independent auditing regressions; these do not establish profitability."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location("cashflow_audit",Path(__file__).resolve().parents[1]/"scripts/validate_cashflow_alpha.py")
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


def fill(ts,side,symbol,qty,price,fee):
    return {"ts_ms":ts,"side":side,"symbol":symbol,"quantity":qty,
            "fill_price":price,"notional":qty*price,"fee":fee}


class IndependentAuditTests(unittest.TestCase):
    def fixture(self):
        # One add/partial-exit campaign makes +22.13; one writeoff loses 10.1.
        fs=[fill(1,"BUY","A",2,10,.2),fill(2,"SELL","A",1,15,.15),
            fill(3,"BUY","A",1,12,.12),fill(4,"SELL","A",2,20,.4),
            fill(5,"BUY","B",1,10,.1)]
        return {"config":{"initial_cash":100},"fills":fs,
                "writeoffs":[{"ts_ms":6,"symbol":"B","quantity":1,"cost_basis_lost":10.1}],
                "equity":[{"ts_ms":86_399_999,"equity":112.03,"cash":112.03,"exposure":0}]}

    def test_campaign_ledger_partial_exit_add_and_writeoff(self):
        r=audit.recompute(self.fixture())
        self.assertEqual(r["metrics"]["closed_campaign_count"],2)
        self.assertAlmostEqual(r["campaigns"][0]["pnl"],22.13)
        self.assertAlmostEqual(r["campaigns"][1]["pnl"],-10.1)
        self.assertAlmostEqual(r["metrics"]["profit_factor"],22.13/10.1)

    def test_detects_misreported_equity_and_invented_inventory(self):
        r=self.fixture();r["equity"][-1]["equity"]+=1
        with self.assertRaisesRegex(AssertionError,"final_equity"):
            audit.recompute(r)
        r=self.fixture();r["fills"][1]["quantity"]=3;r["fills"][1]["notional"]=45
        with self.assertRaisesRegex(AssertionError,"sell_exceeds_inventory"):
            audit.recompute(r)

    def test_yearly_returns_use_previous_close(self):
        r=self.fixture()
        r["equity"]=[
            {"ts_ms":1609459199999,"equity":105,"cash":0,"exposure":1},
            {"ts_ms":1609545599999,"equity":112.03,"cash":112.03,"exposure":0}]
        x=audit.recompute(r)["metrics"]
        self.assertAlmostEqual(x["annual_returns"]["2020"],.05)
        self.assertAlmostEqual(x["annual_returns"]["2021"],112.03/105-1)

    def test_block_bootstrap_deterministic_constant_case(self):
        result=audit.block_bootstrap({str(i):.01 for i in range(24)},repetitions=100)
        self.assertEqual(result["monthly_median_95pct_interval"],[.01,.01])
        self.assertAlmostEqual(result["annualized_geometric_return_95pct_interval"][0],1.01**12-1)

    def test_signal_input_timestamp_and_delayed_buy_enforced(self):
        day=86_400_000
        p={"signal_day_ms":3*day,"observed_ms":4*day,"execute_after_ms":5*day,
           "decisions":[{"max_input_close_ms":4*day-1}],
           "targets":[{"symbol":"A","allow_add":True}]}
        r={"plans":[p],"fills":[fill(5*day,"BUY","A",1,10,0)]}
        self.assertTrue(audit.check_signal_chronology(r)["buy_delay"])
        p["decisions"][0]["max_input_close_ms"]=4*day
        with self.assertRaisesRegex(AssertionError,"future_signal_input"):
            audit.check_signal_chronology(r)
        p["decisions"][0]["max_input_close_ms"]=4*day-1
        r["fills"][0]["ts_ms"]=4*day
        with self.assertRaisesRegex(AssertionError,"buy_without_prior_delayed_plan"):
            audit.check_signal_chronology(r)


if __name__=="__main__":
    unittest.main()
