import copy
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from one_trading.observer import (DAY, Bar, DataError, PublicClient, Record, StopRequired, Store, analyze, complete_weeks, complete_months, parse_bars, scan_once, select_universe)


def candles(server_ms, count=240, slope=0.001, base=100.0, contracting=False):
    first = (server_ms // DAY - count) * DAY
    rows = []
    for i in range(count):
        price = base * (1 + slope) ** i
        width = 0.01 if contracting and i >= count - 14 else 0.02
        rows.append([first + i * DAY, str(price), str(price * (1 + width)), str(price * (1 - width)), str(price), "1000", first + (i + 1) * DAY - 1, "100000", 50, "0", "0", "0"])
    return rows


class FakeClient:
    def __init__(self, server_ms=None, broken_btc=False, stop_symbol=None):
        self.server_ms = server_ms or int(time.time() * 1000)
        self.broken_btc, self.stop_symbol = broken_btc, stop_symbol
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        if path.endswith("/time"):
            data = {"serverTime": self.server_ms}
        elif path.endswith("exchangeInfo"):
            data = {"symbols": [{"symbol": base + "USDT", "baseAsset": base, "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True} for base in ["BTC", "ETH", "JUP", "USDC", "BTCUP"]]}
        elif path.endswith("24hr"):
            data = [{"symbol": base + "USDT", "quoteVolume": "100000000", "closeTime": self.server_ms} for base in ["BTC", "ETH", "JUP", "USDC", "BTCUP"]]
        else:
            symbol = params["symbol"]
            if symbol == self.stop_symbol:
                raise StopRequired("HTTP 451")
            data = candles(self.server_ms, slope=0 if symbol == "BTCUSDT" else 0.001, contracting=True)
            # Improve relative strength in the last 7 days, still far below surge.
            if symbol != "BTCUSDT":
                for i in range(len(data) - 7, len(data)):
                    ratio = 1 + 0.001 * (i - (len(data) - 8))
                    for field in range(1, 5):
                        data[i][field] = str(float(data[i][field]) * ratio)
            if self.broken_btc and symbol == "BTCUSDT":
                data[-1][4] = "NaN"
        return Record(path, params, self.server_ms, json.dumps(data).encode())


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = 20_000 * DAY + 3_600_000
        self.rows = candles(self.now)

    def test_open_bar_discarded_and_closed_exact(self):
        current = copy.deepcopy(self.rows[-1])
        current[0] += DAY
        current[6] += DAY
        bars = parse_bars(self.rows + [current], self.now)
        self.assertEqual(len(bars), 240)
        self.assertEqual(bars[-1].close_ms, self.now // DAY * DAY - 1)

    def test_malformed_inputs_fail_closed(self):
        for mutation in ("nan", "zero", "negative", "gap", "duplicate", "stale", "ohlc", "future", "timestamp", "volume"):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.rows)
                if mutation == "nan": rows[-1][4] = "NaN"
                if mutation == "zero": rows[-1][4] = "0"
                if mutation == "negative": rows[-1][1] = "-1"
                if mutation == "gap": del rows[50]
                if mutation == "duplicate": rows.insert(50, rows[50])
                if mutation == "stale": rows.pop()
                if mutation == "ohlc": rows[-1][3] = "99999"
                if mutation == "future": rows[-1][0] += DAY * 2; rows[-1][6] += DAY * 2
                if mutation == "timestamp": rows[-1][0] = float(rows[-1][0])
                if mutation == "volume": rows[-1][5] = "-1"
                with self.assertRaises(DataError): parse_bars(rows, self.now)

    def test_short_history_rejected(self):
        with self.assertRaises(DataError): parse_bars(self.rows[-99:], self.now)

    def test_weekly_only_calendar_complete(self):
        bars = parse_bars(self.rows, self.now)
        weeks = complete_weeks(bars)
        self.assertGreater(len(weeks), 30)
        self.assertTrue(all((w.close_ms - w.open_ms + 1) == 7 * DAY for w in weeks))
        self.assertTrue(all(((w.open_ms // DAY) + 3) % 7 == 0 for w in weeks))

    def test_btc_missing_and_date_mismatch_fail_closed(self):
        bars = parse_bars(self.rows, self.now)
        self.assertEqual(analyze("ETHUSDT", bars, None)["state"], "SKIP")
        shifted = parse_bars(candles(self.now + DAY), self.now + DAY)
        self.assertEqual(analyze("ETHUSDT", bars, shifted)["reasons"], ["BTC_DATE_MISALIGNMENT"])

    def test_surge_is_never_candidate(self):
        bars = parse_bars(candles(self.now, slope=0.03), self.now)
        btc = parse_bars(candles(self.now, slope=0), self.now)
        result = analyze("ETHUSDT", bars, btc)
        self.assertEqual(result["state"], "SKIP")
        self.assertEqual(result["reasons"], ["NO_SURGE_CHASING"])

    def test_explicit_exclusions_do_not_remove_jup(self):
        fake = FakeClient(self.now)
        exchange, tickers = fake.get("/api/v3/exchangeInfo").data, fake.get("/api/v3/ticker/24hr").data
        selected, _ = select_universe(exchange, tickers, self.now)
        self.assertIn("JUPUSDT", selected)
        self.assertNotIn("USDCUSDT", selected)
        self.assertNotIn("BTCUPUSDT", selected)
        selected, bad = select_universe(exchange, tickers, self.now, symbols=["BTCUSDT", "MISSINGUSDT"])
        self.assertEqual(selected, ["BTCUSDT"])
        self.assertIn("MISSINGUSDT", bad)
        tickers[0]["closeTime"] -= 400_000
        selected, bad = select_universe(exchange, tickers, self.now)
        self.assertNotIn("BTCUSDT", selected)
        self.assertIn("BTCUSDT", bad)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "observer.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_scan_restart_idempotent_and_sha_exact(self):
        fake = FakeClient()
        summary = scan_once(fake, self.store, symbols=["ETHUSDT"])
        self.assertEqual(summary["status"], "OK")
        self.assertGreater(summary["new_events"], 0)
        self.store.close()
        self.store = Store(self.path)
        repeated = scan_once(fake, self.store, symbols=["ETHUSDT"])
        self.assertEqual(repeated["new_events"], 0)
        for row in self.store.db.execute("SELECT raw,sha256 FROM snapshots"):
            self.assertEqual(hashlib.sha256(row["raw"]).hexdigest(), row["sha256"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE decisions SET state='BAD'")

    def test_missing_btc_never_produces_candidates(self):
        summary = scan_once(FakeClient(broken_btc=True), self.store, symbols=["ETHUSDT"])
        self.assertEqual(summary["status"], "DEGRADED")
        self.assertTrue(all(x["state"] == "SKIP" for x in summary["decisions"]))

    def test_permission_stop_rolls_back_partial_scan(self):
        summary = scan_once(FakeClient(stop_symbol="ETHUSDT"), self.store, symbols=["ETHUSDT"])
        self.assertEqual(summary["status"], "STOP_REQUIRED")
        self.assertEqual(summary["decisions"], [])
        self.assertEqual(self.store.status()["counts"]["decisions"], 0)
        self.assertEqual(self.store.status()["last_health"]["status"], "STOP_REQUIRED")

    def test_outcomes_only_future_baseline_all_states_and_idempotent(self):
        base_day = 20_000 * DAY
        for state in ("WATCH", "SKIP", "PREPOSITION_CANDIDATE"):
            self.store.decision({"symbol": "ETHUSDT", "state": state, "bar_close_ms": base_day - 1, "reasons": [state]}, base_day + 1_000)
        self.store.db.commit()
        future_now = base_day + 9 * DAY + 3_600_000
        bars = parse_bars(candles(future_now), future_now)
        btc = parse_bars(candles(future_now, slope=0), future_now)
        with self.store.db:
            count = self.store.record_outcomes({"ETHUSDT": bars, "BTCUSDT": btc}, future_now)
        self.assertEqual(count, 9)
        self.assertEqual(self.store.record_outcomes({"ETHUSDT": bars, "BTCUSDT": btc}, future_now), 0)
        for outcome in self.store.db.execute("SELECT * FROM outcomes"):
            self.assertEqual(outcome["entry_open_ms"], base_day + DAY)
            self.assertGreater(outcome["exit_close_ms"], base_day + DAY)
            self.assertIn("NOT_TRADING_PNL", outcome["payload"])

    def test_no_outcome_if_exact_next_open_unavailable(self):
        base_day = 20_000 * DAY
        self.store.decision({"symbol": "ETHUSDT", "state": "WATCH", "bar_close_ms": base_day - 1, "reasons": []}, base_day + 1_000)
        future_now = base_day + 9 * DAY + 3_600_000
        bars = parse_bars(candles(future_now), future_now)
        bars = [b for b in bars if b.open_ms != base_day + DAY]
        self.assertEqual(self.store.record_outcomes({"ETHUSDT": bars}, future_now), 0)

    def test_no_outcome_using_bar_closed_after_evaluation(self):
        day = 20_000 * DAY
        self.store.decision({"symbol": "ETHUSDT", "state": "WATCH", "bar_close_ms": day - 1, "reasons": []}, day + 1_000)
        later = day + 9 * DAY
        bars = parse_bars(candles(later), later)
        self.assertEqual(self.store.record_outcomes({"ETHUSDT": bars}, day + DAY + 1), 0)


class NetworkSafetyTests(unittest.TestCase):
    def test_trade_endpoints_blocked(self):
        with self.assertRaises(StopRequired): PublicClient().get("/api/v3/order")

    def test_access_and_rate_limits_stop_without_retry(self):
        for code in (401, 403, 407, 418, 429, 451):
            with self.subTest(code=code):
                client = PublicClient(min_delay=0)
                with patch.object(client.opener, "open", side_effect=urllib.error.HTTPError("https://data-api.binance.vision", code, "blocked", {"Retry-After": "30"}, None)) as mocked:
                    with self.assertRaises(StopRequired): client.get("/api/v3/time")
                    self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
