import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from one_trading.batch_analysis import design_diagnostic,historical_contexts,historical_comparison,credit_gate
from one_trading.economic_batches import validate,schema,growth_execution,model_once,BoundedInputs
from one_trading.alpha_loop.protocol import defaults
from one_trading.alpha_loop.reference.evidence import MixtureEvidence
from one_trading.autonomy.store import Store,utc,digest

class EconomicBatchTests(unittest.TestCase):
    def test_design_matches_original_math_and_does_not_loosen_threshold(self):
        result=design_diagnostic(defaults(),.05/6);self.assertEqual(result['planned_maximum_nonoverlapping_units'],56)
        self.assertGreater(result['deterministic_effect_required_per_unit']['incremental'],.015)
        for row in result['effects']:
            x=MixtureEvidence((.1,.25,.5,.75))
            for _ in range(56):x.update(row['capital_net_effect']/.05/2)
            self.assertAlmostEqual(__import__('math').exp(x.log_e),row['incremental_e_at_planned_n'])
        self.assertFalse(result['retrospective_threshold_change'])

    def data(self):
        h=3600000;end=1000*h;d={}
        for s in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            bars=[[t,100,102,99,100.1,1,t+h-1,1,1,1,1,0] for t in range(end-32*24*h,end,h)]
            funding=[dict(symbol=s,fundingTime=t,fundingRate='-0.0001') for t in range(end-33*24*h,end,8*h)]
            d[s]=dict(bars=bars,funding=funding)
        return d

    def test_historical_windows_do_not_overlap_and_have_all_endpoints(self):
        cs=historical_contexts(self.data());self.assertEqual(len(cs),13)
        self.assertTrue(all(a['exit_ms']<b['entry_ms'] for a,b in zip(cs,cs[1:])))
        self.assertTrue(all(c['entry_ms']-c['signal_ms']==7200000 for c in cs))
        self.assertTrue(all(v['funding_source_time']<=c['signal_ms'] for c in cs for v in c['assets'].values()))

    def test_historical_cost_reconciliation_and_missing_ai_not_cash(self):
        d=self.data();cs=historical_contexts(d);cs[0]['assets']['BTCUSDT']['past_24h_return']=-.02
        r=historical_comparison(cs,d,None)
        self.assertIsNone(r['aggregates']['AI_RETROSPECTIVE']['net_usdt'])
        row=next(x for x in r['rows'] if x.get('symbol')=='BTCUSDT')
        self.assertAlmostEqual(row['net_usdt'],1250*(1-.0016)/(1+.0016)-1250)
        self.assertEqual(r['independent_forward_units'],0)

    def test_no_future_price_in_model_context(self):
        d=self.data();before=historical_contexts(d);signal=before[0]['signal_ms']
        changed=copy.deepcopy(d)
        for rows in changed.values():
            for b in rows['bars']:
                if signal<=b[0]<before[0]['exit_ms']+3600000:b[1]=b[4]=999
        self.assertEqual(before[0],historical_contexts(changed)[0])

    def test_schema_rejects_wrong_duplicate_ids_and_symbols(self):
        d={k:'x' for k in schema()['properties']['decisions']['items']['required']};d.update(id='a',action='LONG',symbol='BTCUSDT')
        validate({'decisions':[d]},['a'],['BTCUSDT'])
        for bad in ({'decisions':[d,d]},{'decisions':[dict(d,symbol='CAKEUSDT')]},{'decisions':[dict(d,action=True)]}):
            with self.assertRaises(ValueError):validate(bad,['a'],['BTCUSDT'])

    def observation(self,start,end,first=100,last=110):
        values=lambda n:dict(usdc_supply=n,aave_usdc_debt=n,aave_usdc_deposits=n)
        return dict(status='MEASURED',observed_at=datetime.fromtimestamp(end+10,timezone.utc).isoformat(),
            points=[dict(timestamp=start,values=values(first)),dict(timestamp=end,values=values(last))])

    def test_growth_early_and_overlapping_points_cannot_confirm(self):
        r='2026-10-29T00:00:00+00:00';t=int(datetime.fromisoformat(r).timestamp())
        old=self.observation(t-86400,t);new=self.observation(t,t+86400)
        self.assertFalse(credit_gate(old,[old],r)['allow_entry'])
        self.assertFalse(credit_gate(new,[old,new],r)['allow_entry'])
        overlap=self.observation(t+100,t+86500)
        self.assertFalse(credit_gate(overlap,[new,overlap],r)['allow_entry'])
        later=self.observation(t+86400,t+172800)
        self.assertTrue(credit_gate(later,[new,later],r)['allow_entry'])
        falling=self.observation(t+86400,t+172800,110,100)
        self.assertEqual(credit_gate(falling,[new,falling],r)['state'],'FALSE')

    def test_growth_unknown_is_not_false(self):
        r=credit_gate(dict(status='DATA_INSUFFICIENT',missing=['archive']),[],'2026-10-29T00:00:00Z')
        self.assertEqual(r['state'],'UNKNOWN');self.assertFalse(r['allow_entry'])

    def test_native_growth_delayed_fill_cost_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));now=utc();decision=dict(arm='G_LAGGED',action='LONG',ready_at='2026-01-01T00:00:00Z')
            book=dict(received_at=now,receipt_id='r',mid=100,bids=[[99.99,10000]],asks=[[100.01,10000]],spread_bps=2)
            market=dict(filters=[dict(filterType='LOT_SIZE',minQty='.001',maxQty='9999',stepSize='.001'),dict(filterType='MIN_NOTIONAL',minNotional='5')])
            c=dict(growth_capital_usdt=8000000/1400,growth_hold_days=30)
            try:
                with patch('one_trading.economic_batches.fresh_book',return_value=book) as mock:
                    x=growth_execution(s,c,'x',decision,dict(allow_entry=True),market,.1)
                    y=growth_execution(s,c,'x',decision,dict(allow_entry=True),market,.1)
                self.assertEqual(x,y);self.assertEqual(mock.call_count,1);self.assertEqual(x['status'],'FILLED')
                self.assertLessEqual(-x['fill']['cash_delta_usdt'],c['growth_capital_usdt']*.125+.01)
                self.assertAlmostEqual(x['fill']['fee_usdt'],x['fill']['gross_usdt']*.001)
            finally:s.close()

    def test_lagged_blocks_without_confirmed_economic_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                with patch('one_trading.economic_batches.fresh_book',side_effect=AssertionError('must not request a fill')):
                    x=growth_execution(s,{},'x',dict(arm='G_LAGGED',action='LONG',ready_at=utc()),dict(allow_entry=False),{},.1)
                self.assertEqual(x['status'],'WAIT_ECONOMIC_CONFIRMATION');self.assertIsNone(x['fill'])
            finally:s.close()

    def test_model_external_intent_without_receipt_does_not_respend(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));key=digest(['b','f','model-v1'])
            try:
                s.append('batch_model_intent',key,dict(id=key,batch_id='b',started_at=utc(),input_hash=digest({})))
                with patch('one_trading.economic_batches.invoke',side_effect=AssertionError('must not retry uncertain spend')):
                    r=model_once(s,{'max_model_calls_per_batch':2},'b','f',{},schema(),lambda x:x)
                self.assertEqual(r['status'],'EXTERNAL_STATE_UNCERTAIN')
            finally:s.close()

    def test_result_cache_avoids_network_and_preserves_received_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                s.append('research_batch','b',dict(id='b',started_at=utc()))
                inputs=BoundedInputs(s,'b',dict(max_batch_seconds=720,max_http_requests_per_batch=3))
                first=inputs.cached('x',lambda:{'source':'actual-test-placeholder'})
                self.assertEqual(first,inputs.cached('x',lambda:1/0))
                s.append('http_receipt','r',dict(received_at=utc()))
                with self.assertRaisesRegex(ValueError,'REQUEST_LIMIT'):inputs.cached('y',lambda:{})
            finally:s.close()

    def test_resume_downtime_is_not_active_execution_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                s.append('research_batch','b',dict(id='b',started_at='2026-01-01T00:00:00Z'))
                s.append('trial_receipt','b:x',dict(id='b:x',elapsed_seconds=359))
                with patch('one_trading.economic_batches.time.monotonic',side_effect=[100,100,102]):
                    x=BoundedInputs(s,'b',dict(max_batch_seconds=360,max_http_requests_per_batch=90))
                    x.check()
                    with self.assertRaisesRegex(ValueError,'ACTIVE_TIME_LIMIT'):x.check()
            finally:s.close()

    def test_daily_growth_cannot_stack_existing_policy_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp))
            try:
                s.append('growth_bridge_result','old',dict(id='old',arm='G_AI',status='FILLED'))
                with patch('one_trading.economic_batches.fresh_book',side_effect=AssertionError('no second position')):
                    r=growth_execution(s,{},'new',dict(arm='G_AI',action='LONG',ready_at='2026-01-01T00:00:00Z'),{}, {},.1)
                self.assertEqual(r['status'],'WAIT_EXISTING_POSITION')
                self.assertIsNone(r['fill'])
            finally:s.close()

    def test_revision_preserves_prior_config_and_rejects_unfinished_batch(self):
        from one_trading.economic_batches import revise_implementation
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));old=s.append('batch_config','frozen',dict(id='frozen',source_hash='old',max_model_calls_per_batch=2))
            s.close()
            with patch('one_trading.economic_batches.implementation_hash',return_value='newhash'):
                new=revise_implementation(Path(tmp),'operational capital guard')
            s=Store(Path(tmp))
            try:
                self.assertEqual(s.get('batch_config','frozen'),old)
                self.assertEqual(new['max_model_calls_per_batch'],2)
                s.append('research_batch','unfinished',dict(id='unfinished'))
            finally:s.close()
            with patch('one_trading.economic_batches.implementation_hash',return_value='thirdhash'):
                with self.assertRaisesRegex(ValueError,'UNFINISHED_BATCH'):revise_implementation(Path(tmp),'must not revise')

if __name__=='__main__':unittest.main()
