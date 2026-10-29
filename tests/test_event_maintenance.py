"""PR13 regression fixtures only. No exchange orders or market-performance tests."""
import json,tempfile,unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from one_trading.autonomy.store import Store,utc,instant,digest
from one_trading.economic_batches import growth_service
from one_trading.alpha_loop.reference.research_loop import ExperimentSpec
from one_trading.batch_control import claim_owned,ack_preserving_overrun
import test_batch_transitions as transitions

class MaintenanceTests(unittest.TestCase):
    def setup_case(self,path,due=False):
        alpha,batch,loop=transitions.TransitionTests().fixture(path)
        s=Store(batch)
        s.append('growth_bridge_result','entry',dict(id='entry',arm='G_AI',status='FILLED',symbol='AAVEUSDT',
            exit_due=(instant(utc())+timedelta(days=-1 if due else 30)).isoformat(),information_cost_usdt=.1,
            fill=dict(average_price=100,quantity=1,cash_delta_usdt=-100,fee_usdt=.1)))
        s.close();return alpha,batch,loop
    def mocks(self,bid=100):
        from contextlib import ExitStack
        stack=ExitStack();stack.enter_context(patch('one_trading.economic_batches.fresh_book',return_value={'bids':[[bid,100]],'asks':[[bid+1,100]],'received_at':utc()}))
        stack.enter_context(patch('one_trading.economic_batches.PublicClient.get',return_value=({'symbols':[{'filters':[]}]},{})))
        stack.enter_context(patch('one_trading.economic_batches.fill',return_value=dict(cash_delta_usdt=99,quantity=1,average_price=99)))
        return stack
    def test_first_observation_and_same_minute_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp));at=utc()
            try:
                with self.mocks(),patch('one_trading.economic_batches.utc',return_value=at):
                    first=growth_service(b);spent=l.engine_status('CAPITAL_GROWTH')['time_spent'];again=growth_service(b)
                self.assertEqual(first,again);self.assertEqual(first['status'],'POSITIONS_OBSERVED');self.assertEqual(first['observed_ids'],['entry'])
                self.assertEqual(l.engine_status('CAPITAL_GROWTH')['time_spent'],spent)
                self.assertEqual(l.db.execute("SELECT count(*) FROM tasks WHERE task_key LIKE 'growth-maintain-%'").fetchone()[0],1)
                self.assertEqual(l.db.execute('SELECT count(*) FROM alpha_registry').fetchone()[0],1)
            finally:l.close()
    def test_time_exit(self):self.check_exit(True,100,'TIME')
    def test_stop_exit(self):self.check_exit(False,97,'STOP')
    def check_exit(self,due,bid,reason):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp),due)
            try:
                with self.mocks(bid):result=growth_service(b)
                s=Store(b)
                try:self.assertEqual(s.get('growth_bridge_exit','entry')['reason'],reason);self.assertEqual(result['exit_ids'],['entry'])
                finally:s.close()
            finally:l.close()
    def test_crash_after_experiment_before_task_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp))
            try:
                with patch(type(l).__module__+'.ResearchLoop.enqueue_task',side_effect=KeyboardInterrupt('between experiment/task')):
                    with self.assertRaises(KeyboardInterrupt):growth_service(b)
                with self.mocks():result=growth_service(b)
                self.assertEqual(result['status'],'POSITIONS_OBSERVED')
                self.assertEqual(l.db.execute("SELECT count(*) FROM experiments WHERE id LIKE 'growth-maintain-%'").fetchone()[0],1)
            finally:l.close()
    def test_claimed_without_receipt_reserves_uncertain_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp))
            try:
                with patch('one_trading.economic_batches.maintain_growth_positions',side_effect=KeyboardInterrupt()):
                    with self.assertRaises(KeyboardInterrupt):growth_service(b)
                with self.mocks():result=growth_service(b)
                self.assertEqual(result['elapsed_seconds'],240);self.assertEqual(l.engine_status('CAPITAL_GROWTH')['time_spent'],240)
            finally:l.close()
    def test_last_exit_before_receipt_recovers(self):self.crash_finish('growth_maintenance_receipt')
    def test_receipt_before_ack_canonical_recovery(self):self.crash_finish('ack')
    def crash_finish(self,point):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp),True);original=Store.append
            def append(store,kind,key,body):
                if kind==point:raise KeyboardInterrupt(point)
                return original(store,kind,key,body)
            try:
                target='one_trading.economic_batches.ack_preserving_overrun' if point=='ack' else 'one_trading.autonomy.store.Store.append'
                kwargs={'side_effect':KeyboardInterrupt(point)} if point=='ack' else {'new':append}
                with self.mocks(),patch(target,**kwargs):
                    with self.assertRaises(KeyboardInterrupt):growth_service(b)
                with patch('one_trading.economic_batches.fresh_book',side_effect=AssertionError('already closed')):result=growth_service(b)
                self.assertEqual(result['accounting']['status'],'ACKNOWLEDGED')
                s=Store(b)
                try:
                    self.assertEqual(len(s.all('growth_bridge_exit')),1)
                    receipt=s.all('growth_maintenance_receipt')[0];task=l.get_task(receipt['id'])
                    self.assertEqual(json.loads(task['result_json']),receipt['result'])
                finally:s.close()
            finally:l.close()
    def test_halted_sidecar_does_not_block_existing_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp),True)
            try:
                l.register_experiment(ExperimentSpec('side','side','CAPITAL_GROWTH',1,utc(),(instant(utc())+timedelta(days=1)).isoformat(),0,240,'HISTORICAL'))
                l.record_data('side',digest({}));l.enqueue_task('side','side','research',input_hash=digest({}),cash_units=0,time_seconds=240)
                t=claim_owned(l,['side']);ack_preserving_overrun(l,t,{},241);before=l.get_task('side')
                with self.mocks():result=growth_service(b)
                self.assertEqual(result['exit_ids'],['entry']);self.assertEqual(l.get_task('side'),before)
                self.assertEqual(l.engine_status('CAPITAL_GROWTH')['time_reserved'],240);self.assertEqual(l.engine_status('CAPITAL_GROWTH')['engine_halted'],1)
            finally:l.close()
    def test_failed_quote_and_budget_do_not_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp))
            try:
                with patch('one_trading.economic_batches.fresh_book',side_effect=ValueError('missing')):result=growth_service(b)
                self.assertEqual(result['status'],'MAINTENANCE_ERROR');self.assertEqual(l.get_task(result['id'])['state'],'FAILED')
            finally:l.close()
    def test_advance_retry_identity_changes_only_on_progress_or_registered_gate(self):
        from one_trading.batch_control import advance_action
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp));s=Store(a)
            try:
                c=dict(arms=['CF_SEMANTIC'],max_model_attempts=3,model_retry_seconds=[30,60],decision_validity_seconds=600)
                at=utc();case=dict(id='x',cutoff=at);callid=digest(['x:CF_SEMANTIC','attempt',1])
                s.append('model_call',callid,dict(id=callid,completed_at=at,status='FAILED'))
                with patch('one_trading.batch_control.utc',return_value=at):first=advance_action(s,c,case)
                with patch('one_trading.batch_control.utc',return_value=(instant(at)+timedelta(seconds=10)).isoformat()):same=advance_action(s,c,case)
                with patch('one_trading.batch_control.utc',return_value=(instant(at)+timedelta(seconds=31)).isoformat()):ready=advance_action(s,c,case)
                self.assertEqual(first,same);self.assertNotEqual(first,ready);self.assertTrue(ready['retry_gates']['CF_SEMANTIC']['eligible'])
                with patch('one_trading.batch_control.utc',return_value=(instant(at)+timedelta(seconds=601)).isoformat()):expired=advance_action(s,c,case)
                self.assertTrue(expired['decision_window_expired'])
            finally:s.close();l.close()

    def test_maintenance_does_not_postpone_research(self):
        from one_trading.hypothesis_loop import run_queue
        with tempfile.TemporaryDirectory() as tmp:
            a,b,l=self.setup_case(Path(tmp));s=Store(b)
            s.append('research_batch','recent',dict(id='recent',kind='POSITION_MAINTENANCE_NOT_RESEARCH',started_at=utc()));s.close()
            try:
                with self.assertRaisesRegex(ValueError,'ACTUAL_PARENT_RESULT_REQUIRED'):run_queue(b)
                s=Store(b)
                try:self.assertEqual(len(s.all('research_request')),1)
                finally:s.close()
            finally:l.close()

if __name__=='__main__':unittest.main()
