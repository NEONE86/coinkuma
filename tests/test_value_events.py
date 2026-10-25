import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from one_trading.observer import DAY
from one_trading.value_evidence import EvidenceStore, abi_bytes, uni_created, timestamp, unixfs_cid, sha
from one_trading.value_event_study import clocks, schedule, select_controls, wilson


def event(eid='e1',pub='2025-01-02T23:30:00Z',symbol='AAVEUSDT',direct=True):
    return dict(event_id=eid,symbol=symbol,published_at=pub,first_seen_at='2026-10-25T11:00:00Z',
                source_time_proven=True,direct_value_accrual=direct,executed_at=None)


class EvidenceTests(unittest.TestCase):
    def test_clock_lag_crosses_midnight_and_never_backdates_collector(self):
        c=clocks(event())
        self.assertEqual(c['reconstructed_day'],timestamp('2025-01-03T00:00:00Z'))
        self.assertEqual(c['collector_day'],timestamp('2026-10-25T00:00:00Z'))

    def test_requires_timezone(self):
        with self.assertRaisesRegex(ValueError,'timezone'):
            timestamp('2025-01-01T00:00:00')

    def test_execution_future_never_selects_announcement(self):
        a=event();b=dict(a,executed_at='2026-01-01T00:00:00Z')
        self.assertEqual(schedule([a]),schedule([b]))

    def test_unproven_and_incidental_cannot_join(self):
        a=event();a['source_time_proven']=False
        self.assertIsNone(clocks(a)['reconstructed_day'])
        self.assertEqual(schedule([event(direct=False)])[0]['exclusion'],'QUALITATIVE_OR_INCIDENTAL')

    def test_complete_proof_receipt_is_required_for_collector_clock(self):
        a=event();a['evidence_complete_at']='2026-10-26T00:00:00Z'
        self.assertEqual(clocks(a)['collector_day'],timestamp('2026-10-26T00:00:00Z'))

    def test_overlap_suppressed_without_consulting_outcomes(self):
        a=event();b=event('e2','2025-06-01T00:00:00Z');c=event('e3','2025-07-04T00:00:00Z')
        s=schedule([c,b,a])
        self.assertIsNone(s[0]['exclusion'])
        self.assertEqual(s[1]['exclusion'],'DUPLICATE_EPISODE')
        self.assertIsNone(s[2]['exclusion'])

    def test_era_purge_and_maturity(self):
        self.assertEqual(schedule([event(pub='2024-11-01T00:00:00Z')])[0]['exclusion'],'ERA_BOUNDARY_PURGE')
        self.assertEqual(schedule([event(pub='2026-07-01T00:00:00Z')])[0]['exclusion'],'IMMATURE_180D')

    def test_strict_collector_has_no_retrospective_entries(self):
        self.assertEqual(schedule([event()],mode='collector')[0]['exclusion'],'OUTSIDE_STUDY_PERIOD')

    def test_entry_waits_for_completed_day_then_d_plus_two(self):
        s=schedule([event()])[0]
        self.assertEqual(timestamp(s['earliest_hypothetical_entry']),s['day']+2*DAY)

    def test_control_selection_does_not_read_any_future_fields(self):
        case=dict(day=1,symbol='A',liquidity=2e6,history_days=300,liquidity_bin=0,age_bin=0)
        cohort=[dict(case,symbol=s,liquidity=q) for s,q in [('B',2.1e6),('C',1.8e6),('D',2.9e6),('E',1.2e6)]]
        self.assertEqual([r['symbol'] for r in select_controls(case,cohort)],['B','C','D'])
        self.assertEqual([r['symbol'] for r in select_controls(case,cohort,{'B'})],['C','D','E'])
        for r in cohort:r['delayed']={'180d_2x':{'hit':r['symbol']=='B'}}
        self.assertEqual([r['symbol'] for r in select_controls(case,cohort)],['B','C','D'])
        self.assertEqual([r['symbol'] for r in select_controls(case,cohort,failures_only=True)],['C','D','E'])

    def test_wilson_not_degenerate_at_zero_or_one(self):
        self.assertGreater(wilson(0,2)[1],0.5)
        self.assertLess(wilson(2,2)[0],0.5)
        self.assertIsNone(wilson(0,0))

    def test_raw_abi_preserves_original_bytes_and_rejects_bad_offset(self):
        word=lambda x:x.to_bytes(32,'big')
        text=b'# burn UNI\n\xff'
        data=word(93)+b'\0'*(7*32)+word(9*32)+word(len(text))+text
        pid,raw=uni_created('0x'+data.hex())
        self.assertEqual((pid,raw),(93,text))
        with self.assertRaises(ValueError):uni_created('0x'+(word(1)*9).hex())
        with self.assertRaises(ValueError):abi_bytes(word(1000),0)

    def test_ipfs_cid_is_unixfs_wrapped_not_plain_file_hash(self):
        from one_trading.value_evidence import base58
        b=b'token economics'
        self.assertNotEqual(unixfs_cid(b),base58(b'\x12\x20'+hashlib.sha256(b).digest()))
        self.assertEqual(unixfs_cid(b),unixfs_cid(b))
        with self.assertRaises(ValueError):unixfs_cid(b'x'*262145)

    def test_cache_tamper_fails_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'raw').mkdir();b=b'original';digest=sha(b)
            (p/'raw'/'x.bin').write_bytes(b'changed')
            (p/'sources.json').write_text(json.dumps({'https://eth.blockscout.com/test':{'path':'raw/x.bin','sha256':digest}}))
            with self.assertRaisesRegex(ValueError,'tampered'):
                EvidenceStore(p,offline=True).get('https://eth.blockscout.com/test')

    def test_network_target_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=EvidenceStore(tmp,offline=True)
            for url in ['http://eth.blockscout.com/','https://user:pass@eth.blockscout.com/','https://example.com/']:
                with self.assertRaisesRegex(ValueError,'allowlisted'):s.get(url)


if __name__=='__main__':unittest.main()
