"""Cashflow arithmetic regressions; no empirical strategy evidence."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("cashflow_target", Path(__file__).resolve().parents[1] / "scripts" / "analyze_cashflow_target.py")
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


class CashflowTargetTests(unittest.TestCase):
    def test_flat_returns_cannot_fund_withdrawals(self):
        months = [{"month": f"2020-{m:02d}", "daily_factors": [1.0]} for m in range(1,13)]
        result = target.withdrawal_path(months, 20_000_000, 5_000_000)
        self.assertEqual(result["depleted_month_number"], 4)
        self.assertEqual(result["first_shortfall_month_number"], 5)
        self.assertEqual(result["fully_paid_months"], 4)
        self.assertEqual(result["total_paid_krw"], 20_000_000)
        self.assertEqual(result["return_path_drawdown_excluding_withdrawals"], 0)

    def test_25percent_monthly_exactly_funds_5m_on_20m(self):
        months = [{"month": f"2020-{m:02d}", "daily_factors": [1.25]} for m in range(1,13)]
        result = target.withdrawal_path(months, 20_000_000, 5_000_000)
        self.assertTrue(result["funded_all_with_positive_remaining_capital"])
        self.assertEqual(result["ending_capital_krw"], 20_000_000)
        self.assertEqual(result["total_paid_krw"], 60_000_000)

    def test_daily_drawdown_detects_recovery_hidden_in_monthly_return(self):
        months = [{"month": "2020-01", "daily_factors": [.8,1.25]}]
        result = target.withdrawal_path(months, 20_000_000, 0)
        self.assertEqual(result["return_path_15pct_drawdown_first_month_number"], 1)
        self.assertAlmostEqual(result["return_path_drawdown_excluding_withdrawals"], -.2)
        self.assertEqual(result["ending_capital_krw"], 20_000_000)

    def test_window_count_and_no_future_padding(self):
        months = [{"month": str(m), "daily_factors": [1.0]} for m in range(15)]
        result = target.withdrawal_windows(months,20_000_000,5_000_000,12)
        self.assertEqual(result["historical_window_count"],4)
        self.assertEqual(result["depleted_count"],4)
        self.assertEqual(result["windows_where_no_finite_capital_preserves_principal"],4)
        self.assertEqual(result["capital_requirements"][0]["minimum_initial_capital_to_fund_window_ending_at_zero_krw"],60_000_000)

    def test_principal_preservation_capital_uses_full_path(self):
        months = [{"month":str(m),"daily_factors":[1.25]} for m in range(12)]
        result = target.withdrawal_windows(months,20_000_000,5_000_000,12)
        self.assertAlmostEqual(result["principal_preserving_capital_median_on_positive_return_windows_krw"],20_000_000)

    def test_calendar_completeness_and_realized_pnl_are_separate(self):
        start = datetime(2020,1,1,tzinfo=timezone.utc)
        rows = []
        for i in range(32):
            ts = int((start+timedelta(days=i+1)).timestamp()*1000)-1
            rows.append({"ts_ms":ts,"equity":10000+i,"cash":9000})
        run = {"config":{"initial_cash":10000},"equity":rows,
            "fills":[{"ts_ms":rows[5]["ts_ms"],"realized_pnl":7}],
            "writeoffs":[{"ts_ms":rows[6]["ts_ms"],"cost_basis_lost":3}]}
        result = target.monthly_rows(run)
        self.assertTrue(result[0]["full_calendar_month"])
        self.assertFalse(result[1]["full_calendar_month"])
        self.assertAlmostEqual(result[0]["equity_net_return"],.003)
        self.assertEqual(result[0]["realized_pnl_including_writeoffs"],4)


if __name__ == "__main__":
    unittest.main()
