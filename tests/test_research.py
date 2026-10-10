import unittest
from datetime import datetime, timezone
from dataclasses import replace

from one_trading.observer import DAY, Bar, complete_months
from one_trading.research import study


def history(n=450):
    return [Bar(i * DAY, (i + 1) * DAY - 1, 100 + i, 102 + i,
                99 + i, 101 + i, 1000, 100000) for i in range(n)]


class ResearchTests(unittest.TestCase):
    def test_months_reject_partial_boundaries_and_respect_leap_day(self):
        start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        bars = [replace(b, open_ms=b.open_ms + start, close_ms=b.close_ms + start) for b in history(100)]
        months = complete_months(bars)
        self.assertEqual(len(months), 3)
        self.assertEqual((months[1].close_ms + 1 - months[1].open_ms) // DAY, 29)
        self.assertEqual(len(complete_months(bars[1:])), 2)

    def test_future_prices_cannot_change_prior_selection(self):
        bars = history()
        original = study({"BTCUSDT": bars, "TESTUSDT": bars})
        # First signal is day239. Alter every subsequent price, not history.
        changed = [b if i <= 239 else replace(b, open=b.open * 2, high=b.high * 2,
                    low=b.low * 2, close=b.close * 2) for i, b in enumerate(bars)]
        result = study({"BTCUSDT": bars, "TESTUSDT": changed})
        self.assertEqual(original["cohorts"][0]["assets"][0]["selected"],
                         result["cohorts"][0]["assets"][0]["selected"])

    def test_delayed_entry_nonoverlap_and_exact_cost(self):
        bars = history()
        result = study({"BTCUSDT": bars, "TESTUSDT": bars})
        cohorts = result["cohorts"]
        first = cohorts[0]
        self.assertEqual(first["entry_ms"], 241 * DAY)
        self.assertAlmostEqual(first["assets"][0]["net_28d"], bars[268].close / bars[241].open - 1 - 0.002)
        for prev, nxt in zip(cohorts, cohorts[1:]):
            self.assertLess(prev["exit_ms"], nxt["entry_ms"])
        stressed = study({"BTCUSDT": bars, "TESTUSDT": bars}, 0.006)
        self.assertAlmostEqual(first["assets"][0]["net_28d"] - stressed["cohorts"][0]["assets"][0]["net_28d"], 0.004)

    def test_no_signal_means_cash_and_no_invented_active_return(self):
        bars = history()
        summary = study({"BTCUSDT": bars, "TESTUSDT": bars})["summary"]["low_price"]
        self.assertEqual(summary["active_dates"], 0)
        self.assertIsNone(summary["mean_active_net"])
        self.assertEqual(summary["mean_net_per_28d_cohort_cash_when_inactive"], 0)

    def test_missing_benchmark_rejects(self):
        self.assertEqual(study({})["status"], "INSUFFICIENT_HISTORY")


if __name__ == "__main__":
    unittest.main()
