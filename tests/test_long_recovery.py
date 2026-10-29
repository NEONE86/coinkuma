"""Synthetic fault injection with original fill/deadline/reservation guards."""
import tempfile,unittest,json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from recovery_fixtures import fixture,clocks,quotes,book,interrupted_recovery,healthy_paths
import test_batch_transitions as transitions
from one_trading.autonomy.store import Store,utc,instant,digest,process_lock
from one_trading.alpha_loop import worker
from one_trading.alpha_loop.protocol import defaults
from one_trading.batch_control import advance_action,claim_owned,perform_maintenance,QUOTE_ATTEMPT_LIMIT
from one_trading.economic_batches import growth_service

class LongRecoveryTests(unittest.TestCase):
    def test_original_fill_healthy_paths(self):
        for r in healthy_paths().values():
            self.assertTrue(r['identical_retry']);self.assertTrue(r['time_not_double_charged']);self.assertEqual(r['new_alpha_allocations'],0)
    def test_expired_unknown_claim_charged_once_and_successor_only_open(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b,l,at=fixture(Path(temp),True)
            try:
                before=l.db.execute('SELECT count(*) FROM alpha_registry').fetchone()[0]
                with clocks(at),quotes(at),patch('one_trading.economic_batches.maintain_growth_positions',side_effect=KeyboardInterrupt()):
                    with self.assertRaises(KeyboardInterrupt):growth_service(b)
                later=(instant(at)+timedelta(hours=25)).isoformat()
                with clocks(later),quotes(later):
                    result=growth_service(b);spent=l.engine_status('CAPITAL_GROWTH')['time_spent'];growth_service(b)
                self.assertEqual(result['exit_ids'],['entry']);self.assertGreaterEqual(spent,240)
                self.assertEqual(l.engine_status('CAPITAL_GROWTH')['time_spent'],spent)
                self.assertEqual(l.engine_status('CAPITAL_GROWTH')['time_reserved'],0)
                self.assertEqual(l.db.execute('SELECT count(*) FROM alpha_registry').fetchone()[0],before)
                s=Store(b)
                try:self.assertEqual(len(s.all('growth_bridge_exit')),1);self.assertEqual(len(s.all('growth_maintenance_closed')),1)
                finally:s.close()
            finally:l.close()

def recovery_test(point,hours):
    def test(self):
        r=interrupted_recovery(point,hours)
        self.assertTrue(all('error' not in x for x in r['attempts']),r)
        self.assertEqual(r['time_reserved'],0);self.assertEqual(r['exits'],int(point=='receipt_before_ack'))
        self.assertFalse(any(t['state']=='CLAIMED' for t in r['authority_tasks']))
        self.assertEqual(r['acknowledged'],1);self.assertEqual(r['receipts'],1)
    return test
for point in ('intent_before_experiment','experiment_before_task','receipt_before_ack'):
    for name,hours in [('six_minutes',.1),('before_boundary',24-1/3600),('boundary',24),('after_25h',25)]:
        setattr(LongRecoveryTests,'test_'+point+'_'+name,recovery_test(point,hours))

class QuoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.a,self.b,self.l=transitions.TransitionTests().fixture(Path(self.tmp.name));self.s=Store(self.a);self.c=defaults();self.at=utc()
        self.case=dict(id='quotes',cutoff=self.at,starts_at=(instant(self.at)-timedelta(seconds=5)).isoformat(),due_at=(instant(self.at)+timedelta(hours=6)).isoformat(),snapshot_id='s',flow_window_id='f')
        self.s.append('alpha_case','quotes',self.case);self.s.append('alpha_snapshot','s',dict(id='s',cutoff=self.at));self.s.append('flow_window','f',dict(id='f',events=[]))
        for arm in self.c['arms']:
            key='quotes:'+arm
            self.s.append('alpha_decision',key,dict(id=key,case_id='quotes',arm=arm,action='NO_TRADE',symbol='CASH',status='DECIDED',model_call_ids=[],ready_at=(instant(self.at)-timedelta(seconds=5)).isoformat()))
            self.s.append('alpha_entry_result',key+':NATIVE',dict(id=key+':NATIVE',case_id='quotes',arm=arm,track='NATIVE',status='CASH',fill=None))
    def tearDown(self):self.s.close();self.l.close();self.tmp.cleanup()
    def run_at(self,seconds,healthy=False):
        at=(instant(self.at)+timedelta(seconds=seconds)).isoformat()
        with clocks(at),process_lock(self.a):
            action=advance_action(self.s,self.c,self.case);key='advance:quotes:'+digest(action)[:20]
            worker.enqueue(self.l,self.s,'original',key,'research',action)
            task=claim_owned(self.l,[key])
            values=({s:book(at) for s in self.c['universe']},{}) if healthy else ({},{'BTCUSDT':'temporary'})
            with patch('one_trading.alpha_loop.worker.books_now',return_value=values) as fetch:
                receipt=perform_maintenance(self.s,self.l,dict(config=self.c),task,action)[0] if task else None
                return receipt,fetch.call_count,key
    def test_retry_after_restart_success_and_idempotent_cost(self):
        self.assertEqual(self.run_at(0)[1],1)
        self.s.close();self.s=Store(self.a)
        self.assertEqual(self.run_at(120,True)[1],1)
        self.assertEqual(len([x for x in self.s.all('alpha_entry_result') if x['track']=='MATCHED']),4)
        self.run_at(120,True);spent=self.l.engine_status('CASHFLOW')['time_spent'];self.run_at(120,True)
        self.assertEqual(self.l.engine_status('CASHFLOW')['time_spent'],spent)
        self.assertEqual(self.l.engine_status('CASHFLOW')['time_reserved'],0)
    def test_three_failures_terminal_missing_no_cash(self):
        for sec in (0,120,240):self.assertEqual(self.run_at(sec)[1],1)
        self.assertEqual(len(self.s.all('alpha_quote_attempt')),QUOTE_ATTEMPT_LIMIT)
        self.assertIsNone(self.s.get('alpha_quote_terminal','quotes')['metrics'])
        self.assertEqual(self.run_at(360,True)[1],0)
        self.assertEqual(sum(x['track']=='MATCHED' for x in self.s.all('alpha_entry_result')),0)
        self.assertEqual(len(self.s.all('model_call')),0)
    def test_no_fetch_before_gate(self):
        self.run_at(0);self.assertEqual(self.run_at(119,True)[1],0);self.assertEqual(self.run_at(120,True)[1],1)
    def test_exact_validity_boundary_preserved(self):
        self.run_at(0);self.assertEqual(self.run_at(self.c['decision_validity_seconds'],True)[1],1)
    def test_expired_never_late_cash_or_fill(self):
        self.run_at(0);self.assertEqual(self.run_at(self.c['decision_validity_seconds']+1,True)[1],0)
        self.assertEqual(self.s.get('alpha_quote_terminal','quotes')['reason'],'DECISION_WINDOW_CLOSED')
        self.assertEqual(sum(x['track']=='MATCHED' for x in self.s.all('alpha_entry_result')),0)
    def test_crash_consumes_slot_and_recovers_same_claim(self):
        with patch('one_trading.alpha_loop.worker.advance',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):self.run_at(0)
        self.assertEqual(len(self.s.all('alpha_quote_attempt')),1)
        with clocks((instant(self.at)+timedelta(seconds=120)).isoformat()):
            key=self.s.all('alpha_task_action')[-1]['id'];task=self.l.get_task(key);action=self.s.get('alpha_task_action',key)
            with patch('one_trading.alpha_loop.worker.books_now',return_value=({s:book(utc()) for s in self.c['universe']},{})):
                perform_maintenance(self.s,self.l,dict(config=self.c),task,action)
        self.assertEqual(self.l.engine_status('CASHFLOW')['time_reserved'],0)
    def test_concurrent_worker_lock_blocks_duplicate_attempt(self):
        with process_lock(self.a):
            with self.assertRaisesRegex(ValueError,'ALREADY_RUNNING'):self.run_at(0)
        self.assertEqual(len(self.s.all('alpha_quote_attempt')),0)

if __name__=='__main__':unittest.main()
