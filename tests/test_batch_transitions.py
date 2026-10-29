"""Synthetic fault injections; these tests are not market trials or edge evidence."""
import copy,importlib.util,json,tempfile,unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from one_trading.autonomy.store import Store,utc,instant,digest,canonical,atomic_bytes
from one_trading.alpha_loop.worker import initialize as alpha_init,open_loop
from one_trading.alpha_loop.reference.research_loop import ExperimentSpec
from one_trading.economic_batches import initialize,recover,credit_growth,growth_execution,BoundedInputs
from one_trading.batch_control import claim_owned,ack_preserving_overrun,alpha_service
from one_trading.batch_models import policy_costs
from one_trading.hypothesis_loop import propose,run_queue,features,ABSORPTION,DELAY,PANEL

class Inputs:
    def __init__(self):self.cache={}
    def check(self,*a):pass
    def cached(self,k,fn):
        if k not in self.cache:self.cache[k]=fn()
        return self.cache[k]
    def get(self,host,path,params):
        if path.endswith('klines'):return dict(data=[[i,100,100,100,100] for i in range(25)])
        return dict(data={'symbols':[dict(symbol='AAVEUSDT',status='TRADING',filters=[])]})

class TransitionTests(unittest.TestCase):
    def fixture(self,root):
        alpha=root/'alpha';batch=root/'batch';p=alpha_init(alpha);initialize(batch,alpha)
        loop=open_loop(alpha,p['config']);deadline=(instant(utc())+timedelta(days=2)).isoformat()
        loop.register_experiment(ExperimentSpec('original','original','CASHFLOW',1,utc(),deadline,0,5000,'SYNTHETIC'))
        loop.record_data('original',digest({'synthetic':True}));loop.begin_forward('original')
        return alpha,batch,loop

    def test_241_second_overrun_preserved_but_owned_maturity_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha,batch,loop=self.fixture(Path(tmp));s=Store(alpha)
            try:
                deadline=(instant(utc())+timedelta(days=1)).isoformat()
                loop.register_experiment(ExperimentSpec('batch-side','side','CASHFLOW',1,utc(),deadline,0,240,'HISTORICAL'))
                loop.record_data('batch-side',digest({}));loop.enqueue_task('side','batch-side','research',input_hash=digest({}),cash_units=0,time_seconds=240)
                task=claim_owned(loop,['side']);account=ack_preserving_overrun(loop,task,{'status':'DONE'},241)
                self.assertEqual(account['status'],'RECONCILIATION_REQUIRED')
                before=loop.get_task('side');self.assertEqual(before['state'],'CLAIMED')
                self.assertEqual(loop.engine_status('CASHFLOW')['engine_halted'],1)
                self.assertEqual(recover(batch)['status'],'RECONCILIATION_REQUIRED')
                self.assertEqual(recover(batch)['status'],'RECONCILIATION_REQUIRED')
                s.append('alpha_case','due',dict(id='due',starts_at=utc(),due_at=utc()))
                def resolve(store,config,case):return store.append('alpha_outcome',case['id'],dict(case_id=case['id'],synthetic=True))
                with patch('one_trading.alpha_loop.audit.audit_runtime',return_value={}),patch('one_trading.batch_control.alpha.claim_id',return_value='original'),patch('one_trading.batch_control.alpha.resolve_case',side_effect=resolve) as called,patch('one_trading.batch_control.alpha.submit',return_value={}):
                    receipt=alpha_service(alpha)
                self.assertEqual(called.call_count,1);self.assertEqual(receipt['matured_cases'],1)
                self.assertEqual(loop.get_task('side'),before)
                self.assertEqual(loop.engine_status('CASHFLOW')['time_reserved'],240)
                self.assertEqual(loop.engine_status('CASHFLOW')['engine_halted'],1)
                self.assertEqual(loop.db.execute('SELECT count(*) FROM alpha_registry').fetchone()[0],1)
            finally:s.close();loop.close()

    def model_fixture(self,s,action,when,crash_append=False):
        stack=ExitStack()
        for module in ('one_trading.economic_batches','one_trading.batch_models','one_trading.autonomy.model'):
            stack.enter_context(patch(module+'.utc',return_value=when))
        answer={'decisions':[dict(id='b:credit',symbol='AAVEUSDT',action=action,observations='synthetic',inference='synthetic',counter_hypothesis='synthetic',missing='synthetic')]}
        from one_trading.economic_batches import schema
        fields=schema()['properties']['decisions']['items']['required']
        answer['decisions'][0]={k:answer['decisions'][0].get(k,'synthetic') for k in fields}
        def subprocess_run(command,**kw):
            Path(command[command.index('--output-last-message')+1]).write_bytes(canonical(answer))
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        stack.enter_context(patch('one_trading.autonomy.model.shutil.which',return_value='synthetic-codex'))
        stack.enter_context(patch('one_trading.autonomy.model.subprocess.run',side_effect=subprocess_run))
        stack.enter_context(patch('one_trading.economic_batches.credit_observation',return_value=dict(status='DATA_INSUFFICIENT',missing=['synthetic'],observed_at=when)))
        if crash_append:
            original=s.append
            def append(kind,key,data):
                if kind=='model_call':raise KeyboardInterrupt('after completed.json')
                return original(kind,key,data)
            stack.enter_context(patch.object(s,'append',side_effect=append))
        return stack

    def config(self):return dict(registered_at='2026-01-01T00:00:00Z',max_model_calls_per_batch=2,information_cost_usdt_per_call=.1,execution_contract_version=2,growth_hold_days=30,growth_capital_usdt=10000)

    def test_completed_response_recovers_identical_payload_two_minutes_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));inputs=Inputs();at=utc()
            try:
                with self.model_fixture(s,'NO_TRADE',at,True):
                    with self.assertRaises(KeyboardInterrupt):credit_growth(s,self.config(),'b',inputs)
                self.assertEqual(len(list((s.root/'models').glob('*/*/completed.json'))),1)
                self.assertEqual(s.all('model_call'),[])
                later=(instant(at)+timedelta(minutes=2)).isoformat()
                with patch('one_trading.economic_batches.utc',return_value=later),patch('one_trading.economic_batches.invoke',side_effect=AssertionError('must not respend')):
                    result=credit_growth(s,self.config(),'b',inputs)
                call=s.all('model_call')[0]
                self.assertEqual(result['input_hash'],call['input_hash'])
                self.assertEqual(s.all('batch_model_request')[0]['payload']['cutoff'],at)
                self.assertEqual(len(s.all('batch_model_intent')),1)
                # A rehashed replacement input cannot reuse the old decision ID.
                from one_trading.economic_batches import model_once,schema
                with self.assertRaisesRegex(ValueError,'IDENTITY_MISMATCH'):
                    model_once(s,self.config(),'b','credit_growth',dict(cutoff=later),schema(),lambda x:x)
            finally:s.close()

    def test_48h_answer_expires_without_quote_or_fill_and_keeps_input_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));inputs=Inputs();at=utc();original=growth_execution
            def crash(store,config,key,decision,*args):
                if decision['arm']=='G_AI':raise KeyboardInterrupt('after model ledger')
                return original(store,config,key,decision,*args)
            try:
                with self.model_fixture(s,'LONG',at),patch('one_trading.economic_batches.growth_execution',side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):credit_growth(s,self.config(),'b',inputs)
                later=(instant(at)+timedelta(hours=48)).isoformat()
                with patch('one_trading.economic_batches.utc',return_value=later),patch('one_trading.economic_batches.fresh_book',side_effect=AssertionError('expired must not request quote')):
                    result=credit_growth(s,self.config(),'b',inputs)
                ai=next(r for r in result['comparison'] if r['arm']=='G_AI')
                self.assertEqual(ai['status'],'EXPIRED');self.assertIsNone(ai['fill'])
                self.assertEqual(ai['decision']['ready_at'],at)
                self.assertEqual(result['input_hash'],s.all('model_call')[0]['input_hash'])
            finally:s.close()

    def test_validity_state_and_artifact_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));at=utc()
            try:
                decision=dict(arm='G_AI',action='LONG',ready_at=at,expires_at=(instant(at)+timedelta(minutes=5)).isoformat(),gate_hash=digest({'allow_entry':False}))
                with patch('one_trading.economic_batches.fresh_book',side_effect=AssertionError('invalid cause')):
                    self.assertEqual(growth_execution(s,self.config(),'invalid',decision,{'allow_entry':True},{},.1)['status'],'INVALIDATED')
                with self.model_fixture(s,'NO_TRADE',at):credit_growth(s,self.config(),'b',Inputs())
                call=s.all('model_call')[0];folder=s.root/call['artifact_directory']
                answer=json.loads((folder/'answer.json').read_bytes());answer['decisions'][0]['action']='LONG';(folder/'answer.json').write_bytes(canonical(answer))
                with self.assertRaisesRegex(ValueError,'MODEL_ARTIFACT_ANSWER'):credit_growth(s,self.config(),'b',Inputs())
            finally:s.close()

    def test_external_calls_are_refused_before_remaining_time_is_used_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                s.append('research_batch','b',dict(id='b',started_at=utc()))
                with patch('one_trading.economic_batches.time.monotonic',return_value=0):
                    inputs=BoundedInputs(s,'b',dict(max_batch_seconds=360,max_trial_seconds=240,max_http_requests_per_batch=90));inputs.start_trial()
                with patch('one_trading.economic_batches.time.monotonic',return_value=200),patch('one_trading.economic_batches.PublicClient') as client:
                    with self.assertRaisesRegex(ValueError,'TIME_LIMIT'):inputs.get('unused','unused',{})
                    client.assert_not_called()
                    with self.assertRaisesRegex(ValueError,'TIME_LIMIT'):inputs.check(95)
            finally:s.close()

    def seed(self,s):
        h=3600000;end=int(instant(utc()).timestamp()//3600)*h;data={};sources={}
        for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            bars=[[t,100,101,98,99,10,t+h-1,100,10,5,50,0] for t in range(end-32*24*h,end,h)]
            fees=[dict(symbol=symbol,fundingTime=t,fundingRate='-.0001') for t in range(end-33*24*h,end,8*h)]
            sources[symbol]=[];data[symbol]=dict(bars=bars,funding=fees)
            for body in (bars,fees):
                raw=canonical(body);sha=__import__('hashlib').sha256(raw).hexdigest();atomic_bytes(s.root/'raw'/(sha+'.bin'),raw)
                sources[symbol].append(dict(sha256=sha,received_at=utc(),url='synthetic://not-market'))
        s.append('trial_result','parent',dict(id='parent',family='funding_crowding',status='REJECTED',aggregates={},source_receipts=sources))
        return data

    def test_parent_child_result_successor_and_restart_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha,batch,loop=self.fixture(Path(tmp));s=Store(batch)
            try:self.seed(s)
            finally:s.close()
            try:
                result=run_queue(batch,'bounded-test',2);cached=run_queue(batch,'bounded-test',2)
                self.assertEqual(result['counts']['historical_hypothesis_trials'],2)
                self.assertEqual(result['counts']['paper_fills'],0);self.assertEqual(cached['status'],'CACHED')
                s=Store(batch)
                try:
                    specs=s.all('hypothesis_spec');trials=s.all('hypothesis_trial')
                    self.assertEqual([r['mechanism_id'] for r in specs],[ABSORPTION,DELAY,PANEL])
                    self.assertEqual(len(trials),2);self.assertEqual(specs[1]['parent_trial_id'],trials[0]['id'])
                    self.assertEqual(specs[2]['parent_trial_id'],trials[1]['id']);self.assertEqual(run_queue(batch)['status'],'NOT_DUE')
                finally:s.close()
                self.assertEqual(loop.db.execute('SELECT count(*) FROM alpha_registry').fetchone()[0],1)
            finally:loop.close()

    def test_feature_values_ignore_future_prices_and_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:data=self.seed(s)
            finally:s.close()
        before=features(data);changed=copy.deepcopy(data);signal=before[0]['signal_ms']
        for series in changed.values():
            for bar in series['bars']:
                if signal<=bar[0]:bar[1]=bar[4]=999;bar[7]=99999;bar[10]=999
        self.assertEqual(before[0],features(changed)[0])

    def test_typed_trial_restarts_after_ack_before_result_without_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha,batch,loop=self.fixture(Path(tmp));s=Store(batch)
            try:self.seed(s)
            finally:s.close()
            original=Store.append
            def crash(store,kind,key,body):
                if kind=='hypothesis_trial':raise KeyboardInterrupt('ack durable, result not appended')
                return original(store,kind,key,body)
            try:
                with patch.object(Store,'append',new=crash):
                    with self.assertRaises(KeyboardInterrupt):run_queue(batch,'interrupted-child',1)
                with patch('one_trading.hypothesis_loop.evaluate',side_effect=AssertionError('completed receipt must be reused')):
                    result=run_queue(batch,'interrupted-child',1)
                self.assertEqual(result['counts']['historical_hypothesis_trials'],1)
                s=Store(batch)
                try:self.assertEqual(len(s.all('hypothesis_receipt')),1);self.assertEqual(len(s.all('hypothesis_spec')),2)
                finally:s.close()
            finally:loop.close()

    def test_generic_independent_recalculation_and_semantic_tampering(self):
        from one_trading.hypothesis_loop import evaluate
        path=Path(__file__).parents[1]/'scripts/verify_research_transitions.py'
        modspec=importlib.util.spec_from_file_location('generic_audit',path);audit=importlib.util.module_from_spec(modspec);modspec.loader.exec_module(audit)
        with tempfile.TemporaryDirectory() as tmp:
            alpha,batch,loop=self.fixture(Path(tmp));s=Store(batch)
            try:
                self.seed(s);spec=propose(s,'parent');trial=dict(id=spec['id'],completed_at=utc(),**evaluate(s,spec))
                bundle=Path(tmp)/'export';dest=bundle/'raw/batch';dest.mkdir(parents=True)
                for raw in (s.root/'raw').glob('*.bin'):(dest/raw.name).write_bytes(raw.read_bytes())
                audit.recompute(bundle,spec,trial)
                for mutate in ('quantity','duplicate','feature','arm'):
                    changed=copy.deepcopy(trial)
                    if mutate=='quantity':changed['rows'][0]['quantity']+=1
                    elif mutate=='duplicate':changed['rows'].append(changed['rows'][0])
                    elif mutate=='feature':changed['contexts'][0]['assets']['BTCUSDT']['aggressive_sell_share']=.99
                    else:changed['rows'][0]['arm']='CASH'
                    with self.assertRaises(AssertionError):audit.recompute(bundle,spec,changed)
            finally:s.close();loop.close()

    def test_policy_costs_include_cash_wait_and_count_calls_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                for key,status,call in [('cash','CASH','a'),('wait','WAIT_ECONOMIC_CONFIRMATION','b'),('duplicate','EXPIRED','b')]:
                    s.append('growth_bridge_result',key,dict(id=key,arm='G_AI',status=status,decision=dict(model_call_id=call),information_cost_usdt=.1))
                s.append('growth_bridge_result','entry',dict(id='entry',arm='G_AI',status='FILLED',decision=dict(model_call_id='c'),information_cost_usdt=.1,fill=dict(cash_delta_usdt=-100,fee_usdt=.1)))
                s.append('growth_bridge_exit','entry',dict(id='entry',fill=dict(cash_delta_usdt=110),realized_pnl_usdt=9.9))
                costs=policy_costs(s)['arms']['G_AI']
                self.assertAlmostEqual(costs['policy_net_usdt'],9.7);self.assertAlmostEqual(costs['trade_local_reported_pnl_usdt'],9.9)
            finally:s.close()

    def test_model_rule_semantic_tamper_and_duplicate_rows_rejected(self):
        path=Path(__file__).parents[1]/'scripts/verify_economic_batch_bundle.py'
        spec=importlib.util.spec_from_file_location('independent',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'models/batch/m';folder.mkdir(parents=True)
            context=dict(id='case');payload={'earliest_context':context};answer={'rule':'CASH'}
            (folder/'input.json').write_bytes(canonical(payload));(folder/'answer.json').write_bytes(canonical(answer))
            model=dict(id='m',answer=answer,response_hash=digest(answer),input_hash=digest(payload),status='CONNECTED_VALID',tools_enabled=False)
            trial=dict(model_call_id='m',contexts=[context],selected_ai_rule='CASH',rows=[dict(case_id='case',arm=a) for a in ('PRICE_ONLY','FUNDING_RULE','AI_RETROSPECTIVE','CASH')])
            module.linked_funding(root,trial,[model])
            for bad in (dict(trial,selected_ai_rule='PRICE_ONLY'),dict(trial,model_call_id='missing'),dict(trial,rows=trial['rows']+[trial['rows'][0]]),dict(trial,rows=trial['rows'][:-1])):
                with self.assertRaises(AssertionError):module.linked_funding(root,bad,[model])
            with self.assertRaises(AssertionError):module.linked_funding(root,trial,[model,model])

if __name__=='__main__':unittest.main()
