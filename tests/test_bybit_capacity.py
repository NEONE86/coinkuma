"""Capacity admission and adversarial accounting; no market/network calls.

Large admission caps use small actual fixtures. Counter/stat boundary simulations
are explicitly not physical 3GB write or disk endurance tests.
"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from one_trading.ai_native.bybit_forward import Capture, StopCapture, SYMBOLS
from one_trading.ai_native.bybit_audit import audit, AuditError
from tests.test_bybit_audit_independent import BASE, message, rewrite, reconcile_terminal


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def capture(self, cap, name='run'):
        folder = self.root/name
        c = Capture(folder, BASE, 0, duration=10, max_bytes=cap)
        self.addCleanup(lambda: c.close('TEST', BASE+timedelta(seconds=3), 3) if not c.closed else None)
        c.reconnect(BASE, 0)
        for symbol in SYMBOLS:
            c.ingest(message(symbol, 0), BASE, 0)
        c.sample(BASE+timedelta(seconds=1), 1)
        return c, folder

    def test_both_caps_preserve_raw_and_early_termination(self):
        for cap in (1_000_000_000, 3_000_000_000):
            with self.subTest(cap=cap):
                c, folder = self.capture(cap, str(cap))
                raw = (folder/'raw.jsonl').read_bytes()
                c.close('INTERRUPTED', BASE+timedelta(seconds=2), 2)
                result = audit(folder)
                self.assertEqual(result['terminal_reason'], 'INTERRUPTED')
                self.assertFalse(result['complete_24h'])
                self.assertEqual(result['bytes'], sum(p.stat().st_size for p in folder.iterdir()))
                self.assertEqual((folder/'raw.jsonl').read_bytes(), raw)

    def test_default_remains_one_gb(self):
        c = Capture(self.root/'default', BASE, 0)
        self.assertEqual(c.limit, 1_000_000_000)
        c.close('INTERRUPTED', BASE, 0)

    def test_collector_rejects_above_max_before_creating_directory(self):
        folder = self.root/'invalid'
        with self.assertRaises(ValueError):
            Capture(folder, BASE, 0, max_bytes=3_000_000_001)
        self.assertFalse(folder.exists())

    def test_auditor_rejects_rehashed_above_max(self):
        c, folder = self.capture(3_000_000_000)
        c.close('INTERRUPTED', BASE+timedelta(seconds=2), 2)
        rewrite(folder/'events.jsonl', lambda e: e[0].update(max_bytes=3_000_000_001))
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, 'INVALID_MAX_BYTES'):
            audit(folder)

    def test_byte_accounting_tamper_at_each_cap(self):
        for cap in (1_000_000_000, 3_000_000_000):
            c, folder = self.capture(cap, str(cap))
            c.close('INTERRUPTED', BASE+timedelta(seconds=2), 2)
            with (folder/'raw.jsonl').open('ab') as f:
                f.write(b'\n')
            with self.assertRaisesRegex(AuditError, 'BYTE_ACCOUNTING_MISMATCH'):
                audit(folder)

    def test_large_counter_boundary_reserves_terminal_space(self):
        for cap in (1_000_000_000, 3_000_000_000):
            c, folder = self.capture(cap, str(cap))
            raw = (folder/'raw.jsonl').read_bytes()
            actual_used = c.used
            c.used = cap-4096
            with self.assertRaisesRegex(StopCapture, 'STORAGE_QUOTA'):
                c.write('raw', {'would_overflow': True})
            self.assertEqual(c.fatal_stop['used_bytes'], cap-4096)
            self.assertEqual((folder/'raw.jsonl').read_bytes(), raw)
            # Counter simulation only: restore actual bytes before fixture cleanup.
            c.used = actual_used
            c.close('STORAGE_QUOTA', BASE+timedelta(seconds=2), 2)
            self.assertLess(sum(p.stat().st_size for p in folder.iterdir()), 10_000)

    def test_auditor_enforces_declared_cap_with_large_stat_simulation(self):
        for cap in (1_000_000_000, 3_000_000_000):
            c, folder = self.capture(cap, str(cap))
            c.close('INTERRUPTED', BASE+timedelta(seconds=2), 2)
            original = Path.stat
            def simulated(path, *args, **kwargs):
                if path == folder/'raw.jsonl':
                    return SimpleNamespace(st_size=cap+1, st_mode=original(path).st_mode)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'stat', simulated):
                with self.assertRaisesRegex(AuditError, 'STORAGE_CAP_EXCEEDED'):
                    audit(folder)

    def test_actual_small_quota_retains_raw_and_audits(self):
        c, folder = self.capture(10_000)
        with self.assertRaisesRegex(StopCapture, 'STORAGE_QUOTA'):
            for _ in range(100):
                c.ingest(message(SYMBOLS[0], 1), BASE+timedelta(seconds=1), 1)
        raw = (folder/'raw.jsonl').read_bytes()
        c.close('STORAGE_QUOTA', BASE+timedelta(seconds=2), 2)
        self.assertEqual((folder/'raw.jsonl').read_bytes(), raw)
        self.assertEqual(audit(folder)['terminal_reason'], 'STORAGE_QUOTA')
        self.assertLessEqual(sum(p.stat().st_size for p in folder.iterdir()), 10_000)

    def test_raised_cap_cannot_validate_forged_quota(self):
        c, folder = self.capture(10_000)
        with self.assertRaises(StopCapture):
            for _ in range(100):
                c.ingest(message(SYMBOLS[0], 1), BASE+timedelta(seconds=1), 1)
        c.close('STORAGE_QUOTA', BASE+timedelta(seconds=2), 2)
        rewrite(folder/'events.jsonl', lambda e: e[0].update(max_bytes=3_000_000_000))
        reconcile_terminal(folder)
        with self.assertRaisesRegex(AuditError, 'FATAL_STOP_QUOTA_EVIDENCE'):
            audit(folder)
