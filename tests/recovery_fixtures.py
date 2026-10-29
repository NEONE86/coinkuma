"""Synthetic, offline PR14 diagnostics; not market evidence or production incidents.

Run with Python -B and TMPDIR outside any Git tree. All authority contracts remain
active. Network acquisition and wall clocks alone are mocked; fill is real code.
"""
import json
import sys
import tempfile
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / "tests")]
from test_batch_transitions import TransitionTests
from one_trading.autonomy.store import Store, utc, instant, digest
from one_trading.economic_batches import growth_service
from one_trading.alpha_loop import worker
from one_trading.alpha_loop.protocol import defaults
from one_trading.alpha_loop.reference.research_loop import ResearchLoop
from one_trading.batch_control import advance_action, claim_owned, perform_maintenance

FILTERS = [dict(filterType="LOT_SIZE", minQty=".001", maxQty="100000", stepSize=".001"),
           dict(filterType="MIN_NOTIONAL", minNotional="5")]


def fixture(root, due=False):
    alpha, batch, authority = TransitionTests().fixture(root)
    store = Store(batch)
    at = utc()
    try:
        store.append("growth_bridge_result", "entry", dict(
            id="entry", arm="G_AI", status="FILLED", symbol="AAVEUSDT",
            exit_due=(instant(at) + timedelta(days=-1 if due else 30)).isoformat(),
            information_cost_usdt=.1,
            fill=dict(average_price=100, quantity=1, cash_delta_usdt=-100.1, fee_usdt=.1)))
    finally:
        store.close()
    return alpha, batch, authority, at


def book(at, mid=100):
    return dict(bids=[[mid-.01, 100000]], asks=[[mid+.01, 100000]], mid=mid,
                spread_bps=.02/mid*10000, received_at=at, receipt_id="synthetic-quote")


def clocks(at):
    original = worker.open_loop
    def opened(*args, **kwargs):
        loop = original(*args, **kwargs)
        loop.clock = lambda: instant(at)
        return loop
    stack = ExitStack()
    for module in ("one_trading.economic_batches", "one_trading.autonomy.store",
                   "one_trading.alpha_loop.trading", "one_trading.alpha_loop.worker",
                   "one_trading.batch_control"):
        stack.enter_context(patch(module + ".utc", return_value=at))
    stack.enter_context(patch("one_trading.economic_batches.open_loop", side_effect=opened))
    return stack


def quotes(at, mid=100):
    stack = ExitStack()
    stack.enter_context(patch("one_trading.economic_batches.fresh_book", return_value=book(at, mid)))
    stack.enter_context(patch("one_trading.economic_batches.PublicClient.get",
                             return_value=({"symbols": [{"filters": FILTERS}]}, {})))
    return stack


def healthy_paths():
    results = {}
    for label, due, mid in (("observe", False, 100), ("time_exit", True, 100), ("stop_exit", False, 97)):
        with tempfile.TemporaryDirectory() as temp:
            alpha, batch, authority, at = fixture(Path(temp), due)
            try:
                before_alpha = authority.db.execute("SELECT count(*) FROM alpha_registry").fetchone()[0]
                with clocks(at), quotes(at, mid):
                    first = growth_service(batch)
                    time_once = authority.engine_status("CAPITAL_GROWTH")["time_spent"]
                    second = growth_service(batch)
                store = Store(batch)
                try:
                    exits = store.all("growth_bridge_exit")
                    results[label] = dict(status=first["status"], identical_retry=(first == second),
                        exit_count=len(exits), exit_reason=exits[0]["reason"] if exits else None,
                        actual_fill_fee=exits[0]["fill"]["fee_usdt"] if exits else None,
                        time_not_double_charged=authority.engine_status("CAPITAL_GROWTH")["time_spent"] == time_once,
                        new_alpha_allocations=authority.db.execute("SELECT count(*) FROM alpha_registry").fetchone()[0]-before_alpha)
                finally:
                    store.close()
            finally:
                authority.close()
    return results


def interrupted_recovery(point, hours):
    with tempfile.TemporaryDirectory() as temp:
        alpha, batch, authority, at = fixture(Path(temp), due=point == "receipt_before_ack")
        try:
            target = {
                "intent_before_experiment": "one_trading.alpha_loop.reference.research_loop.ResearchLoop.register_experiment",
                "experiment_before_task": "one_trading.alpha_loop.reference.research_loop.ResearchLoop.enqueue_task",
                "receipt_before_ack": "one_trading.economic_batches.ack_preserving_overrun",
            }[point]
            with clocks(at), quotes(at), patch(target, side_effect=KeyboardInterrupt("synthetic process interruption")):
                try:
                    growth_service(batch)
                except KeyboardInterrupt:
                    pass
            later = (instant(at) + timedelta(hours=hours)).isoformat()
            attempts = []
            with clocks(later), quotes(later):
                for _ in range(2):
                    try:
                        outcome = growth_service(batch)
                        attempts.append(dict(status=outcome["status"], accounting=outcome.get("accounting")))
                    except Exception as exc:
                        attempts.append(dict(error=type(exc).__name__ + ": " + str(exc)))
            store = Store(batch)
            try:
                return dict(point=point, outage_hours=hours, attempts=attempts,
                    intents=len(store.all("growth_maintenance_intent")),
                    acknowledged=len(store.all("growth_maintenance_ack")),
                    receipts=len(store.all("growth_maintenance_receipt")),
                    exits=len(store.all("growth_bridge_exit")),
                    authority_states=[dict(row) for row in authority.db.execute("SELECT id,state FROM experiments WHERE id LIKE 'growth-maintain-%'")],
                    authority_tasks=[dict(row) for row in authority.db.execute("SELECT task_key,state FROM tasks WHERE task_key LIKE 'growth-maintain-%'")],
                    time_reserved=authority.engine_status("CAPITAL_GROWTH")["time_reserved"])
            finally:
                store.close()
        finally:
            authority.close()


def advance_transient_quote_identity():
    """Isolate original action -> authority -> original advance; no audit bypass.

    Pre-existing synthetic decisions/native records are inputs. This is not an
    end-to-end audit-valid market cohort. The quote fetch is the transient fault.
    """
    with tempfile.TemporaryDirectory() as temp:
        alpha, batch, authority = TransitionTests().fixture(Path(temp))
        store = Store(alpha)
        try:
            c = defaults()
            at = utc()
            case = dict(id="quote-retry", starts_at=(instant(at)-timedelta(seconds=5)).isoformat(),
                        cutoff=at, due_at=(instant(at)+timedelta(hours=6)).isoformat(),
                        snapshot_id="synthetic-snapshot", flow_window_id="synthetic-flow")
            store.append("alpha_case", case["id"], case)
            store.append("alpha_snapshot", case["snapshot_id"], dict(id=case["snapshot_id"], cutoff=at))
            store.append("flow_window", case["flow_window_id"], dict(id=case["flow_window_id"], events=[]))
            for arm in c["arms"]:
                key = case["id"]+":"+arm
                store.append("alpha_decision", key, dict(id=key, case_id=case["id"], arm=arm,
                    action="NO_TRADE", symbol="CASH", status="DECIDED", model_call_ids=[],
                    ready_at=(instant(at)-timedelta(seconds=5)).isoformat()))
                store.append("alpha_entry_result", key+":NATIVE", dict(id=key+":NATIVE", case_id=case["id"],
                    arm=arm, track="NATIVE", status="CASH", fill=None))
            with clocks(at):
                action = advance_action(store, c, case)
                key = "advance:"+case["id"]+":"+digest(action)[:20]
                worker.enqueue(authority, store, "original", key, "research", action)
                task = claim_owned(authority, [key])
                with patch("one_trading.alpha_loop.worker.books_now", return_value=({}, {"BTCUSDT":"synthetic temporary quote failure"})) as fetch:
                    receipt, accounting = perform_maintenance(store, authority, dict(config=c), task, action)
                    first_fetches = fetch.call_count
            later = (instant(at)+timedelta(seconds=120)).isoformat()
            with clocks(later):
                retry_action = advance_action(store, c, case)
                retry_key = "advance:"+case["id"]+":"+digest(retry_action)[:20]
                worker.enqueue(authority, store, "original", retry_key, "research", retry_action)
                retry_task = claim_owned(authority, [retry_key])
                matched_before = sum(row.get("track")=="MATCHED" for row in store.all("alpha_entry_result"))
                # A control call proves current inputs permit recovery once the
                # dispatcher actually calls original advance. No new model calls.
                with patch("one_trading.alpha_loop.worker.books_now", return_value=({symbol:book(later) for symbol in c["universe"]}, {})) as healthy_fetch:
                    worker.advance(store, c, case)
                    control_fetches = healthy_fetch.call_count
                matched_after = sum(row.get("track")=="MATCHED" for row in store.all("alpha_entry_result"))
            return dict(first_fetches=first_fetches, first_task_state=authority.get_task(key)["state"],
                        same_action_after_120_seconds=(action==retry_action), retry_task_claimed=bool(retry_task),
                        matched_records_before_control=matched_before, matched_records_after_control=matched_after,
                        control_fetches=control_fetches,
                        qualification="Synthetic all-CASH decisions; demonstrates missed matched diagnostic retry, not missed real profitable trade.")
        finally:
            store.close()
            authority.close()
