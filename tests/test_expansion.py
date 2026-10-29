import copy,json,tempfile,unittest,time
from pathlib import Path
from unittest.mock import patch
import test_batch_transitions as fixtures
from one_trading.autonomy.store import Store,utc,digest
from one_trading.historical_expansion import trade,price,H,load,calculate,ABSORPTION,DELAY
from one_trading.expansion_io import owned,Inputs

class ExpansionTests(unittest.TestCase):
    def test_cash_and_costed_fixed_horizons(self):
        bars={'BTCUSDT':{n*H:[n*H,100] for n in range(27)}}
        for h in (4,24):
            r=trade(bars,'BTCUSDT',0,h);self.assertAlmostEqual(r['net_usdt'],-2500*.0016/1.0016)
            self.assertEqual(r['capital_occupied_hours'],26);self.assertEqual(r['exposure_hours'],h)
            self.assertEqual(trade(bars,None,0,h)['trades'],0)
        self.assertIsNone(price({'assets':{'BTCUSDT':{'past_24h_return':0},'ETHUSDT':{'past_24h_return':.1}}}))
    def test_missing_or_corrupt_raw_cannot_be_imputed(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(t)
            try:
                binding=[dict(symbol='BTCUSDT',kind='bars',receipt=dict(sha256='a'*64,received_at=utc()))]
                with self.assertRaises(FileNotFoundError):load(s,{},binding)
                (s.root/'raw').mkdir(exist_ok=True);(s.root/'raw'/('a'*64+'.bin')).write_bytes(b'[]')
                with self.assertRaisesRegex(ValueError,'RAW_HASH'):load(s,{},binding)
            finally:s.close()
    def test_clock_overlap_cross_mechanism_clusters_and_periods(self):
        assets={s:dict(past_24h_return=-.1,aggressive_sell_share=.8,volume_ratio=2,hour_return=0) for s in ('BTCUSDT','ETHUSDT','SOLUSDT')}
        cs=[dict(signal_ms=i*H,assets=assets) for i in (25,26,51)]
        data={s:dict(bars=[[n*H,100] for n in range(80)],funding=[]) for s in assets}
        spec=dict(historical_range=[25*H,52*H],reused_range=[100*H,100*H],blocks=[dict(id='a',start_ms=25*H,end_ms=26*H),dict(id='b',start_ms=26*H,end_ms=52*H)],classification='SYNTHETIC')
        with patch('one_trading.historical_expansion.load',return_value=data),patch('one_trading.historical_expansion.contexts',return_value=(cs,[])):
            r=calculate(None,spec,[])
        self.assertEqual(len(r['events']),2);self.assertEqual(len(r['cluster_scores']),1)
        self.assertEqual(r['cluster_scores'][0]['block'],'a');self.assertEqual(r['verdicts']['short_4h'],'INCONCLUSIVE')
        self.assertEqual(r['events'][0]['arms']['ECONOMIC_SELECTION']['4']['trades'],r['events'][0]['arms']['ECONOMIC_SELECTION']['24']['trades'])
    def test_interrupted_operation_charged_once_without_rerunning(self):
        with tempfile.TemporaryDirectory() as t:
            alpha,batch,loop=fixtures.TransitionTests().fixture(Path(t));s=Store(batch);spec=dict(registered_at=utc(),deadline='2099-01-01T00:00:00+00:00')
            try:
                with patch('one_trading.expansion_io.ack_preserving_overrun',side_effect=RuntimeError('crash before ack')):
                    with self.assertRaises(RuntimeError):owned(s,'extension-test',spec,30,lambda _:dict(status='COMPLETED'))
                again=owned(s,'extension-test',spec,30,lambda _:self.fail('must not replay'))
                self.assertEqual(again['result']['status'],'COMPLETED');self.assertEqual(loop.get_task('extension-test')['state'],'DONE')
                self.assertEqual(loop.engine_status('CASHFLOW')['time_reserved'],0)
                owned(s,'extension-test',spec,30,lambda _:self.fail('must remain cached'))
            finally:s.close();loop.close()
    def test_http_intent_counts_unknown_attempt_and_rolling_cap(self):
        with tempfile.TemporaryDirectory() as t:
            s=Store(t)
            try:
                s.append('extension_http_intent','lost',dict(id='lost',operation_id='prior',at=utc()))
                i=Inputs(s,'new',time.monotonic(),60,max_attempts=1,rolling=True)
                with self.assertRaisesRegex(ValueError,'SHARED_ROLLING'):i.check()
            finally:s.close()
    def test_short_terminal_bar_quarantined_without_extending_it(self):
        import hashlib
        with tempfile.TemporaryDirectory() as t:
            s=Store(t)
            try:
                row=[0,100,100,100,100,1,H-2,100,1,1,40];raw=json.dumps([row]).encode();sha=hashlib.sha256(raw).hexdigest()
                (s.root/'raw').mkdir(exist_ok=True);(s.root/'raw'/(sha+'.bin')).write_bytes(raw)
                bindings=[dict(symbol='BTCUSDT',kind='bars',receipt=dict(sha256=sha,received_at=utc()))];q=[]
                with self.assertRaisesRegex(ValueError,'QUARANTINE'):load(s,{},bindings)
                data=load(s,{},bindings,q);self.assertEqual(data['BTCUSDT']['bars'],[]);self.assertEqual(q[0]['close_ms'],H-2)
            finally:s.close()

class PilotTests(unittest.TestCase):
    def test_cached_ack_precedes_expired_registration_guards(self):
        with tempfile.TemporaryDirectory() as t:
            alpha,batch,loop=fixtures.TransitionTests().fixture(Path(t));s=Store(batch);spec=dict(registered_at=utc(),deadline='2099-01-01T00:00:00+00:00')
            try:
                with patch('one_trading.expansion_io.ack_preserving_overrun',side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError):owned(s,'old-ack',spec,30,lambda _:dict(status='DONE'))
                with patch('one_trading.alpha_loop.reference.research_loop.ResearchLoop.record_data',side_effect=AssertionError('must not re-register completed operation')):
                    result=owned(s,'old-ack',spec,30,lambda _:self.fail('must not rerun'))
                self.assertTrue(result['success']);self.assertEqual(loop.get_task('old-ack')['state'],'DONE')
            finally:s.close();loop.close()
    def test_each_http_attempt_respects_absolute_stop(self):
        from one_trading.receipt_pilot import PilotInputs
        with tempfile.TemporaryDirectory() as t:
            s=Store(t)
            try:
                i=PilotInputs(s,'pilot-test',time.monotonic(),100,deadline=0)
                with self.assertRaisesRegex(ValueError,'ABSOLUTE_COLLECTION_DEADLINE'):i.check()
            finally:s.close()
    def test_future_start_and_old_missed_design_preserved(self):
        from one_trading.receipt_pilot import register,slots
        with tempfile.TemporaryDirectory() as t:
            alpha,batch,loop=fixtures.TransitionTests().fixture(Path(t));s=Store(batch)
            try:
                old=s.append('event_followup_design','old',dict(id='old',start_signal_at='2000-01-01T00:00:00+00:00',first_feature_receipt_due='2000-01-01T00:02:00+00:00'))
                spec=register(s);self.assertGreater(spec['start_signal_at'],spec['registered_at']);self.assertEqual(s.get('event_followup_design','old'),old)
                self.assertEqual(s.get('followup_missed_start','old')['status'],'MISSED_NOT_RETROACTIVE');self.assertLessEqual(len(list(slots(spec))),84)
                self.assertEqual(register(s),spec)
            finally:s.close();loop.close()
    def test_deadline_missing_and_stop_without_network(self):
        from one_trading.receipt_pilot import register,service
        from one_trading.historical_expansion import ms,iso
        with tempfile.TemporaryDirectory() as t:
            alpha,batch,loop=fixtures.TransitionTests().fixture(Path(t));s=Store(batch)
            try:spec=register(s)
            finally:s.close()
            with patch('one_trading.receipt_pilot.utc',return_value=iso(ms(spec['stop_at'])+1)),patch('one_trading.receipt_pilot.Inputs.get',side_effect=AssertionError('no network after stop')):
                r=service(batch);self.assertEqual(r['status'],'STOPPED');self.assertGreater(r['missing'],0)
                again=service(batch);self.assertEqual(again['missing'],r['missing'])
            loop.close()
    def test_unknown_inflight_operation_is_terminal_charged(self):
        from one_trading.alpha_loop.reference.research_loop import ExperimentSpec
        from one_trading.batch_control import claim_owned
        with tempfile.TemporaryDirectory() as t:
            alpha,batch,loop=fixtures.TransitionTests().fixture(Path(t));s=Store(batch);spec=dict(registered_at=utc(),deadline='2099-01-01T00:00:00+00:00')
            try:
                loop.register_experiment(ExperimentSpec('lost-op','test-lost','CASHFLOW',1,spec['registered_at'],spec['deadline'],0,30,'HISTORICAL'))
                loop.record_data('lost-op',digest(spec));loop.enqueue_task('lost-op','lost-op','research',input_hash=digest(spec),cash_units=0,time_seconds=30);claim_owned(loop,['lost-op'])
                result=owned(s,'lost-op',spec,30,lambda _:self.fail('uncertain operation must not replay'))
                self.assertFalse(result['success']);self.assertEqual(result['elapsed_seconds'],30);self.assertEqual(loop.engine_status('CASHFLOW')['time_reserved'],0)
            finally:s.close();loop.close()
