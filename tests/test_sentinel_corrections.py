"""Audit regressions. Synthetic transports only; never a new market observation."""
import hashlib
import json
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

from sentinel_test_support import isolate_claims
from one_sentinel.engine import Observer
from one_sentinel.demo import T, SYMBOL, sequence, mini, bar, book, metadata
from one_sentinel.paper import account
from one_sentinel.store import raw_usage
from one_sentinel.transport import public_get, validate_approval, send_webhook


def stamp(wall, mono, session='fixture-session'):
    return dict(wall=wall, monotonic=mono, session_id=session)


class Corrections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'r'
        self.o = Observer(self.root)
        isolate_claims(self,self.o.l.root.parent)
    def tearDown(self):
        self.o.close(); self.tmp.cleanup()
    def put(self, source, at, value, **kw):
        return self.o.ingest(source, json.dumps(value).encode(), at, at, **kw)
    def opened(self):
        for source, at, data in sequence()[:5]: self.put(source, at, data)
    def approval(self):
        c = self.o.l.get('contract', 'v1')
        a = {k:c[k] for k in ('policy_hash', 'account_hash', 'source_hash')}
        a.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_NO_NETWORK', public_data=True, start_at=T-1,
                 stop_at=T+1000, model_cash_budget=0, live_orders=False,
                 channel_url_sha256=hashlib.sha256(b'https://example.invalid/channel').hexdigest())
        validate_approval(self.o, a, T)
        return a
    def test_stop_exit_has_new_atomic_risk_alert(self):
        self.opened(); n = len(self.o.l.all('outbox'))
        r = self.put('mini', T+6, mini(T+6, '94'))
        d = self.o.l.all('decision')[-1]; out = self.o.l.all('outbox')[-1]
        self.assertEqual(d['action'], 'EXIT'); self.assertEqual(len(self.o.l.all('outbox')), n+1)
        self.assertEqual(out['body']['decision_id'], d['id'])
        self.assertIn(r['id'], out['body']['evidence_ids'])
        self.assertEqual(out['received_at'], T+6)
        self.assertEqual(out['body']['holding_qty'], '9.995')
    def test_reduce_horizon_and_exposure_each_alert(self):
        self.opened(); self.put('mini', T+6, mini(T+6, '96'))
        self.assertEqual(self.o.l.all('outbox')[-1]['body']['action'], 'REDUCE')
        self.o.tick(T+5+self.o.policy['horizon_seconds'])
        self.assertEqual(self.o.l.all('outbox')[-1]['body']['risk_reason'], 'HORIZON_EXPIRED')
    def test_exit_supersedes_reduce_and_sells_once_after_delay(self):
        self.opened(); self.put('mini', T+6, mini(T+6, '96')); reduce = self.o.l.all('decision')[-1]
        self.put('mini', T+7, mini(T+7, '94')); exit_d = self.o.l.all('decision')[-1]
        self.assertEqual(exit_d['action'], 'EXIT')
        self.assertEqual(self.o.l.get('execution', reduce['id'])['reason'], 'RISK_PRIORITY_CANCELLED')
        self.put('book', T+8, book('94')); self.assertEqual(len(self.o.l.all('fill')), 1)
        self.o.close(); self.o = Observer(self.root)
        self.put('book', T+9, book('94')); self.put('book', T+9, book('94'))
        self.assertEqual([f['action'] for f in self.o.l.all('fill')], ['ENTER','EXIT'])
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'], '0.000')
    def test_decision_outbox_crash_rolls_back_both(self):
        self.opened(); before = len(self.o.l.all('decision'))
        with patch('one_sentinel.engine.risk_outbox', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.put('mini', T+6, mini(T+6, '94'))
        self.o.close(); self.o = Observer(self.root)
        self.assertEqual(len(self.o.l.all('decision')), before)
        self.put('mini', T+6, mini(T+6, '94'))
        self.assertEqual(self.o.l.all('outbox')[-1]['body']['action'], 'EXIT')
    def test_risk_transition_dedup_and_independent_new_risk(self):
        self.opened(); self.put('mini', T+6, mini(T+6,'96'))
        n = len(self.o.l.all('outbox'))
        self.put('mini', T+6, mini(T+6,'96')); self.o.tick(T+6)
        self.assertEqual(len(self.o.l.all('outbox')), n)
        self.put('mini', T+7, mini(T+7,'94'))
        self.assertEqual(len(self.o.l.all('outbox')), n+1)
    def test_saved_ack_recovers_after_expiry_without_network(self):
        self.opened(); a = self.approval(); row = self.o.l.all('outbox')[0]
        with self.o.l.transaction():
            self.o.l.add('send_intent', row['id'], dict(id=row['id'],at=T+3))
            self.o.l.add('provider_receipt', row['id'], dict(id=row['id'],ack_at=T+4,phone_received_at=None,actual_delivery=False))
        class Never:
            def open(self,*args,**kw): raise AssertionError('NO_NETWORK')
        send_webhook(self.root,'https://example.invalid/channel',clock=lambda:a['stop_at']+1,opener=Never())
        self.assertEqual(len(self.o.l.all('delivery')),1)
        self.assertIsNone(self.o.l.all('delivery')[0]['phone_received_at'])
    def partial(self, mode):
        self.approval(); self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=100
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n):
                if mode=='partial': raise IncompleteRead(b'partial-body',20)
                raise TimeoutError('unknown bytes in socket')
        class Fake:
            def open(self,*args,**kw): return Response()
        with self.assertRaises((IncompleteRead,TimeoutError)):public_get(self.o,'metadata',clock=lambda:T,opener=Fake())
        return self.o.l.all('http_result')[-1]
    def test_known_partial_eof_is_preserved_and_charged(self):
        r=self.partial('partial'); self.assertEqual(r['bytes'],12)
        self.assertEqual((self.root/'raw'/(r['raw_hash']+'.bin')).read_bytes(),b'partial-body')
        self.assertGreaterEqual(raw_usage(self.o.l,T),12)
    def test_unknown_read_timeout_keeps_upper_bound_across_restart(self):
        r=self.partial('timeout'); self.assertEqual(r['bytes'],0)
        self.assertEqual(r['accounting_state'],'UNCERTAIN')
        self.assertEqual(raw_usage(self.o.l,T),100)
        self.o.close();self.o=Observer(self.root);self.assertEqual(raw_usage(self.o.l,T),100)
    def test_mixed_frame_retains_valid_symbol_with_envelope(self):
        data=mini(T-.1)+[dict(mini(T+20)[0],s='BADUSDT')]
        raw=json.dumps(dict(stream='!miniTicker@arr',data=data)).encode()
        r=self.o.ingest('mini',raw,T,T)
        self.assertIn(SYMBOL,self.o.l.get('input',r['id'])['parsed'])
        self.assertNotIn('BADUSDT',self.o.l.get('input',r['id'])['parsed'])
        self.assertEqual((self.root/'raw'/(r['sha256']+'.bin')).read_bytes(),raw)
    def test_reused_without_clock_evidence_remains_unverifiable(self):
        r=self.put('mini',T,mini(T-.1),classification='REAL_REUSED',provenance={'original_receipt_id':'original','original_received_at':T})
        self.assertIsNone(self.o.l.get('input',r['id']))
        revision=self.o.l.get('interpretation',r['id'])
        self.assertEqual(revision['items'][0]['state'],'UNVERIFIABLE')
        self.assertEqual(self.o.l.get('receipt',r['id'])['received_at'],T)
    def test_calibrated_clock_skew_large_future_and_uncertainty(self):
        from one_sentinel.clocks import calibration, assess
        cfg=self.o.policy['clock_model']; sent=stamp(T,10); recv=stamp(T+.1,10.1)
        c=calibration(int((T+.65)*1000),sent,recv,'receipt',cfg)
        now=stamp(T+.2,10.2)
        self.assertEqual(assess(T+.5,now,c,cfg)['state'],'VALID')
        self.assertEqual(assess(T+100,now,c,cfg)['state'],'FUTURE')
        self.assertEqual(assess(T+.8,now,c,cfg)['state'],'UNCERTAIN')
    def test_clock_wall_jumps_and_restart_do_not_reuse_epoch(self):
        from one_sentinel.clocks import calibration, assess
        cfg=self.o.policy['clock_model']; c=calibration(int(T*1000),stamp(T,1),stamp(T+.1,1.1),'r',cfg)
        for wall in (T-100,T+100):
            self.assertEqual(assess(T-.1,stamp(wall,1.2),c,cfg)['state'],'UNCERTAIN')
        self.assertEqual(assess(T-.1,stamp(T+.2,1.2,'new-session'),c,cfg)['state'],'UNVERIFIABLE')
    def test_budget_limit_creates_persistent_halt(self):
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=1
        with self.assertRaisesRegex(ValueError,'RAW_DAILY_LIMIT'):self.put('mini',T,mini(T))
        self.assertTrue(self.o.l.all('observation_halt'))


if __name__=='__main__': unittest.main()
