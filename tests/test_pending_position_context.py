"""Regression: delayed plans must apply only to the positions they observed."""
import copy
import unittest

from one_trading.observer import DAY
from one_trading.portfolio import Book, make_plan
from test_learning_execution import config, plan


class FixedModel:
    def predict(self, _):
        return (.05, .04)

    def metadata(self):
        return {"fixture": True}


def row(extended=False):
    return {"symbol": "AAAUSDT", "x": [], "support_stop": 90.,
            "metrics": {"BTC_regime": "ON", "return_7d": .13 if extended else 0.,
                        "return_30d": 0., "extension_above_SMA20": 0.,
                        "holding_low": True, "base_width_20d": .2, "last_close": 100.}}


class PendingPositionTests(unittest.TestCase):
    def setUp(self):
        self.c = config()
        self.book = Book(10000.)
        self.prices = {"AAAUSDT": 100.}

    def buy(self, ts=DAY, id="buy"):
        p = plan(id=id, due=ts)
        p["position_context"] = {}
        self.book.apply_plan(p, self.prices, ts, self.c)

    def test_delayed_flat_plan_does_not_sell_a_later_entry(self):
        first = make_plan([row()], FixedModel(), self.book, self.prices, DAY, self.c)
        # Still flat when this second plan was authored; an extended new entry
        # is excluded, but an existing winner would be held by the same policy.
        second = make_plan([row(True)], FixedModel(), self.book, self.prices, 2*DAY, self.c)
        self.book.apply_plan(first, self.prices, 2*DAY, self.c)
        before = copy.deepcopy(self.book.positions)
        notes = self.book.apply_plan(second, self.prices, 3*DAY, self.c)
        self.assertEqual(self.book.positions, before)
        self.assertIn("AAAUSDT:POSITION_NOT_OBSERVED_BY_PLAN", notes)
        self.assertFalse(any(f["side"] == "SELL" for f in self.book.fills))

    def test_pending_add_cannot_reopen_stopped_position(self):
        self.buy()
        add = make_plan([row()], FixedModel(), self.book, self.prices, 2*DAY, self.c)
        self.book.monitor_quotes({"AAAUSDT": 85.}, 2*DAY, self.c)
        self.assertFalse(self.book.positions)
        notes = self.book.apply_plan(add, self.prices, 3*DAY, self.c)
        self.assertFalse(self.book.positions)
        self.assertIn("AAAUSDT:STALE_POSITION_INTENT_SKIPPED", notes)

    def test_old_exit_cannot_sell_replacement_position(self):
        self.buy()
        old_exit = {"id": "old-exit", "execute_after_ms": 4*DAY,
                    "expires_ms": 5*DAY-1, "targets": [],
                    "position_context": {"AAAUSDT": DAY}}
        self.book.liquidate(self.prices, 2*DAY, self.c, "TEST_CLOSE")
        self.buy(3*DAY, "replacement")
        qty = self.book.positions["AAAUSDT"].qty
        self.book.apply_plan(old_exit, self.prices, 4*DAY, self.c)
        self.assertEqual(self.book.positions["AAAUSDT"].qty, qty)

    def test_fresh_explicit_exit_still_executes(self):
        self.buy()
        exit_plan = {"id": "fresh-exit", "execute_after_ms": 2*DAY,
                     "expires_ms": 3*DAY-1, "targets": [],
                     "position_context": {"AAAUSDT": DAY}}
        self.book.apply_plan(exit_plan, self.prices, 2*DAY, self.c)
        self.assertFalse(self.book.positions)
        self.assertEqual(self.book.fills[-1]["reason"], "ROTATE_OR_INVALIDATE")

    def test_flat_entry_plan_cannot_become_unplanned_add(self):
        stale = plan("flat-entry", due=2*DAY)
        stale["position_context"] = {}
        self.buy()
        before = copy.deepcopy(self.book.positions)
        self.book.apply_plan(stale, {"AAAUSDT": 95.}, 2*DAY, self.c)
        self.assertEqual(self.book.positions, before)

    def test_contextless_plan_rejected_without_mutating_book(self):
        p = plan()
        p.pop("position_context", None)
        before = self.book.to_dict()
        with self.assertRaisesRegex(ValueError, "position_context_required"):
            self.book.apply_plan(p, self.prices, DAY, self.c)
        self.assertEqual(self.book.to_dict(), before)

    def test_risk_halt_overrides_stale_plan_protection(self):
        self.buy()
        self.book.peak = 20000.
        p = plan("stale", due=2*DAY)
        p["position_context"] = {}
        self.book.apply_plan(p, self.prices, 2*DAY, self.c)
        self.assertFalse(self.book.positions)
        self.assertTrue(self.book.halted)

    def test_plan_captures_position_identity_and_restart_preserves_it(self):
        self.buy()
        p = make_plan([row()], FixedModel(), self.book, self.prices, 2*DAY, self.c)
        self.assertEqual(p["position_context"], {"AAAUSDT": DAY})
        restored = Book.from_dict(self.book.to_dict())
        notes = restored.apply_plan(p, self.prices, 3*DAY, self.c)
        self.assertFalse(any("STALE" in n or "NOT_OBSERVED" in n for n in notes))
        self.assertIn("AAAUSDT", restored.positions)


if __name__ == "__main__":
    unittest.main()
