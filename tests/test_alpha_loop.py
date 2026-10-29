"""Synthetic chronology, execution and independent-audit counterexamples."""
import hashlib,json,math,sqlite3,tempfile,unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from one_trading.autonomy.store import Store,utc,instant,digest,canonical,atomic_bytes
from one_trading.alpha_loop.protocol import defaults
from one_trading.alpha_loop.observations import normalize_flow,fit_exposures,state_at,economic_label
from one_trading.alpha_loop.decisions import price_decision,model_decision,retrieve_memory,validate_answer
from one_trading.alpha_loop.trading import fill,attempt_entry,maintain_exits
from one_trading.alpha_loop.worker import initialize,verify_authority,metrics_for,open_loop,new_case,resolve_case,submit
from one_trading.alpha_loop.audit import audit_runtime

class AlphaLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root);self.addCleanup(lambda:self.store.close())
        self.c=defaults();self.now=utc()
        self.filters=[dict(filterType='LOT_SIZE',minQty='.001',maxQty='100000',stepSize='.001'),dict(filterType='MIN_NOTIONAL',minNotional='5')]
    def book(self,mid=98,when=None):
        return dict(bids=[[mid-.01,100000]],asks=[[mid+.01,100000]],mid=mid,
                    spread_bps=.02/mid*10000,received_at=when or utc(),receipt_id='quote')
    def snapshot(self):
        end=int(instant(self.now).timestamp())*1000-3600000
        fits={s:dict(beta=.5,alpha_hourly=0,sigma_hourly=.01,anchor_price=100,
                    benchmark_symbols=[x for x in self.c['universe'] if x!=s],fit_end_ms=end) for s in self.c['universe']}
        books={s:self.book(98 if s=='ETHUSDT' else 100) for s in self.c['universe']}
        return dict(id='s',cutoff=self.now,universe=self.c['universe'],fits=fits,books=books,
            state=state_at(fits,books,self.now),errors={},filters={s:self.filters for s in self.c['universe']})
    def decision(self,arm='CF_PRICE_ONLY'):
        return dict(id='case:'+arm,case_id='case',arm=arm,status='DECIDED',action='LONG',symbol='ETHUSDT',
                    ready_at=(instant(self.now)-timedelta(seconds=4)).isoformat(),model_call_ids=[])
    def case(self):
        return dict(id='case',starts_at=(instant(self.now)-timedelta(seconds=5)).isoformat(),
                    due_at=(instant(self.now)+timedelta(hours=6)).isoformat(),snapshot_id='s')
    def test_bybit_side_is_position_side_not_order_side(self):
        x=dict(topic='allLiquidation.ETHUSDT',data=[dict(T=int(instant(self.now).timestamp()*1000),s='ETHUSDT',S='Buy',v='2',p='100')])
        e=normalize_flow(x,self.now,self.c['universe'])[0]
        self.assertEqual(e['forced_side'],'SELL');self.assertFalse(e['price_is_executable'])
        x['data'][0]['S']='Sell';self.assertEqual(normalize_flow(x,self.now,self.c['universe'])[0]['forced_side'],'BUY')
    def test_flow_future_clock_and_wrong_asset_rejected(self):
        x=dict(topic='allLiquidation.ETHUSDT',data=[dict(T=int(instant(self.now).timestamp()*1000)+60000,s='ETHUSDT',S='Buy',v='2',p='100')])
        with self.assertRaises(ValueError):normalize_flow(x,self.now,self.c['universe'])
        x['data'][0]['T']-=60000;x['data'][0]['s']='BTCUSDT'
        with self.assertRaises(ValueError):normalize_flow(x,self.now,self.c['universe'])
    def test_fit_uses_only_completed_pre_event_bars(self):
        n=5;end=3600000*10
        series={s:[[end-(n+1-i)*3600000,0,0,0,100+2*j+i+math.sin(i*(j+1)),0,end-(n-i)*3600000-1] for i in range(n+1)] for j,s in enumerate(self.c['universe'])}
        f=fit_exposures(series,self.c['universe'],end,n);self.assertEqual(f['ETHUSDT']['observations'],5)
        series['ETHUSDT'][-1][6]+=1
        with self.assertRaises(ValueError):fit_exposures(series,self.c['universe'],end,n)
    def test_price_baseline_monitors_candidate_outside_ai_choice(self):
        s=self.snapshot();s['state']['ETHUSDT']['z']=-2;s['state']['SOLUSDT']['z']=-4
        d=price_decision(s,self.c);self.assertEqual(d['symbol'],'SOLUSDT')
        self.assertIn('BTCUSDT',d['rejected_alternatives'])
    def test_unsupported_short_is_not_free_trade(self):
        s=self.snapshot()
        for x in s['state'].values():x['z']=3
        d=price_decision(s,self.c);self.assertEqual(d['action'],'NO_TRADE')
        self.assertEqual(set(d['rejected_alternatives'].values()),{'SHORT_UNSUPPORTED'})
    def test_native_baseline_survives_opportunity_disappearance(self):
        s=self.snapshot();case=self.case();d=self.decision()
        first={x:self.book(98 if x=='ETHUSDT' else 100) for x in self.c['universe']}
        with patch('one_trading.alpha_loop.trading.books_now',return_value=(first,{})):
            original=attempt_entry(self.store,self.c,case,s,d)
        self.assertEqual(original['status'],'FILLED')
        later={x:self.book(101 if x=='ETHUSDT' else 100) for x in self.c['universe']}
        with patch('one_trading.alpha_loop.trading.books_now',return_value=(later,{})):
            delayed=attempt_entry(self.store,self.c,case,s,self.decision('CF_SEMANTIC'))
        self.assertEqual(delayed['status'],'REJECTED_OPPORTUNITY');self.assertIn('NO_EXECUTABLE_GAP',delayed['reason'])
        self.assertEqual(self.store.get('alpha_entry_result',d['id']+':NATIVE'),original)
    def test_predecision_quote_and_duplicate_position(self):
        s=self.snapshot();d=self.decision();case=self.case()
        bad={x:self.book(98 if x=='ETHUSDT' else 100,(instant(self.now)-timedelta(minutes=1)).isoformat()) for x in self.c['universe']}
        with patch('one_trading.alpha_loop.trading.books_now',return_value=(bad,{})):
            r=attempt_entry(self.store,self.c,case,s,d)
        self.assertEqual(r['status'],'REJECTED_OPPORTUNITY');self.assertIn('PRE_DECISION_QUOTE',r['reason'])
        again=attempt_entry(self.store,self.c,case,s,d);self.assertEqual(r,again)
        self.assertEqual(len(self.store.all('alpha_entry_result')),1)
    def test_lot_fee_spread_slippage_and_depth(self):
        b=self.book();r=fill(b,'BUY',self.c,1.23456,self.filters)
        self.assertEqual(r['quantity'],1.234);self.assertGreater(r['fee_usdt'],0)
        self.assertGreater(r['spread_depth_cost_usdt'],0);self.assertGreater(r['slippage_cost_usdt'],0)
        self.assertEqual(r['funding_status'],'NOT_APPLICABLE_SPOT')
        b['asks'][0][1]=.01
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_DEPTH'):fill(b,'BUY',self.c,1,self.filters)
    def test_unknown_is_not_false_and_price_does_not_set_economic_label(self):
        due=(instant(self.now)-timedelta(seconds=1)).isoformat()
        a=dict(coverage='CONTIGUOUS_SUBSCRIBED_WINDOW',events=[dict(symbol='ETHUSDT',forced_side='SELL',quantity_base=10)])
        b=dict(coverage='CONTIGUOUS_SUBSCRIBED_WINDOW',started_at=self.now,events=[dict(symbol='ETHUSDT',forced_side='SELL',quantity_base=4)])
        self.assertEqual(economic_label(a,b,'ETHUSDT','SELL',self.now,due),'TRUE')
        b['events'][0]['quantity_base']=8
        self.assertEqual(economic_label(a,b,'ETHUSDT','SELL',self.now,due),'FALSE')
        b['coverage']='INCOMPLETE';self.assertEqual(economic_label(a,b,'ETHUSDT','SELL',self.now,due),'UNKNOWN')
        self.assertEqual(economic_label(a,None,'ETHUSDT','SELL',self.now,(instant(self.now)+timedelta(hours=1)).isoformat()),'PENDING')
    def test_memory_is_real_mature_received_and_audited(self):
        past=(instant(self.now)-timedelta(hours=1)).isoformat();future=(instant(self.now)+timedelta(hours=1)).isoformat()
        for key,due,origin in [('good',past,'FORWARD_PAPER'),('future',future,'FORWARD_PAPER'),('fake',past,'SYNTHETIC')]:
            self.store.append('event_memory',key,dict(id=key,due_at=due,received_at=past,origin=origin,independent_audit_passed=True))
        cutoff=utc();self.assertEqual([x['id'] for x in retrieve_memory(self.store,cutoff)],['good'])
        self.assertEqual(retrieve_memory(self.store,past),[])
    def test_cold_memory_and_missing_cause_not_fabricated_cash(self):
        s=self.snapshot();flow={'events':[dict(symbol='ETHUSDT',forced_side='SELL')]}
        d=model_decision(self.store,self.c,{'id':'case'},'CF_SEMANTIC_MEMORY',s,flow)
        self.assertEqual(d['status'],'MEMORY_UNAVAILABLE');self.assertIsNone(d['action'])
        d=model_decision(self.store,self.c,{'id':'case'},'CF_SEMANTIC',s,{'events':[]})
        self.assertIn('DATA_BLOCKED',d['status']);self.assertEqual(len(self.store.all('model_call')),0)
    def test_model_success_never_resampled(self):
        s=self.snapshot();flow={'events':[dict(symbol='ETHUSDT',forced_side='SELL')]}
        answer=dict(action='NO_TRADE',symbol='CASH',classification='UNKNOWN',p_reversion=.4,
                    observed='o',source_claim='s',assumptions='a',counter_hypothesis='c',invalidation='i',rationale='r')
        def invoke(store,c,key,payload,**kw):return store.append('model_call',key,dict(id=key,status='CONNECTED_VALID',answer=answer,completed_at=utc()))
        with patch('one_trading.alpha_loop.decisions.invoke',side_effect=invoke) as m:
            a=model_decision(self.store,self.c,{'id':'case'},'CF_SEMANTIC',s,flow)
            b=model_decision(self.store,self.c,{'id':'case'},'CF_SEMANTIC',s,flow)
        self.assertEqual(a,b);self.assertEqual(m.call_count,1)
    def test_failed_model_retains_attempt_and_recovers_without_baseline_delay(self):
        s=self.snapshot();flow={'events':[dict(symbol='ETHUSDT',forced_side='SELL')]}
        c=dict(self.c,model_retry_seconds=[0,0])
        answer=dict(action='NO_TRADE',symbol='CASH',classification='UNKNOWN',p_reversion=.4,
                    observed='o',source_claim='s',assumptions='a',counter_hypothesis='c',invalidation='i',rationale='r')
        attempts=[]
        def invoke(store,c,key,payload,**kw):
            attempts.append(key)
            return store.append('model_call',key,dict(id=key,status='MODEL_TIMEOUT' if len(attempts)==1 else 'CONNECTED_VALID',answer=answer,completed_at=utc()))
        baseline=price_decision(s,c)
        with patch('one_trading.alpha_loop.decisions.invoke',side_effect=invoke):d=model_decision(self.store,c,{'id':'case'},'CF_SEMANTIC',s,flow)
        self.assertEqual(d['status'],'DECIDED');self.assertEqual(len(set(attempts)),2)
        self.assertEqual(self.store.get('model_call',attempts[0])['status'],'MODEL_TIMEOUT')
        self.assertLessEqual(instant(baseline['ready_at']),instant(d['ready_at']))
    def test_failed_model_is_unavailable_not_zero_return(self):
        self.store.append('alpha_decision','case:CF_SEMANTIC',dict(id='case:CF_SEMANTIC',case_id='case',arm='CF_SEMANTIC',
            status='MODEL_BLOCKED',action=None,model_call_ids=['failed']))
        value=metrics_for(self.store,self.c,self.case(),'CF_SEMANTIC','NATIVE')
        self.assertIsNone(value['metrics']);self.assertEqual(value['status'],'UNAVAILABLE')
    def test_real_native_execution_finishes_before_model_invocation(self):
        from one_trading.alpha_loop.worker import advance
        s=self.snapshot();s['state']['ETHUSDT']['z']=-2
        self.store.append('alpha_snapshot','s',s)
        self.store.append('flow_window','flow',dict(id='flow',events=[dict(symbol='ETHUSDT',forced_side='SELL')]))
        case=dict(self.case(),flow_window_id='flow')
        events=[]
        def entry(store,c,case,snapshot,d,**kwargs):events.append(('entry',d['arm']));return {'status':'CASH'}
        def model(store,c,case,arm,snapshot,flow):
            events.append(('model',arm));return store.append('alpha_decision','case:'+arm,
                dict(id='case:'+arm,case_id='case',arm=arm,status='MODEL_BLOCKED',action=None,symbol=None,ready_at=utc(),model_call_ids=[]))
        with patch('one_trading.alpha_loop.worker.attempt_entry',side_effect=entry),patch('one_trading.alpha_loop.worker.model_decision',side_effect=model),patch('one_trading.alpha_loop.worker.books_now',return_value=({}, {'blocked':'synthetic'})),patch('one_trading.alpha_loop.worker.maintain_exits'),patch('one_trading.alpha_loop.worker.time.sleep'):
            advance(self.store,self.c,case)
        self.assertLess(events.index(('entry','CF_PRICE_ONLY')),events.index(('model','CF_SEMANTIC')))
    def test_invalid_boolean_probability(self):
        a=dict(action='NO_TRADE',symbol='CASH',classification='UNKNOWN',p_reversion=True,
               observed='o',source_claim='s',assumptions='a',counter_hypothesis='c',invalidation='i',rationale='r')
        with self.assertRaises(ValueError):validate_answer(a,self.c['universe'])
    def test_ambiguous_external_call_not_blindly_retried(self):
        s=self.snapshot();flow={'events':[dict(symbol='ETHUSDT',forced_side='SELL')]};aid=digest(['case:CF_SEMANTIC','attempt',1])
        self.store.append('alpha_model_intent',aid,dict(id=aid))
        with patch('one_trading.alpha_loop.decisions.invoke') as m:
            d=model_decision(self.store,self.c,{'id':'case'},'CF_SEMANTIC',s,flow)
        self.assertEqual(d['status'],'MODEL_EXTERNAL_STATE_UNCERTAIN');m.assert_not_called()
    def test_authority_config_is_frozen_and_cannot_reset(self):
        # The caller-specified path is pinned, so copying a DB cannot reset global alpha.
        self.store.close();initialize(self.root);self.store=Store(self.root)
        p=verify_authority(self.store);self.assertFalse(p['config']['live_trading_enabled'])
        with self.assertRaises(ValueError):self.store.append('alpha_protocol','frozen',dict(p,config_hash='bad'))
        a=json.loads((self.root/'authority.json').read_text());a['runtime']=str(self.root/'copy')
        (self.root/'authority.json').write_text(json.dumps(a))
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):verify_authority(self.store)
    def test_independent_audit_detects_rehashed_flow_mutation(self):
        initialize(self.root)
        data=dict(topic='allLiquidation.ETHUSDT',data=[dict(T=int(instant(self.now).timestamp()*1000),s='ETHUSDT',S='Buy',v='2',p='100')])
        b=canonical(data);h=hashlib.sha256(b).hexdigest();atomic_bytes(self.root/'raw'/(h+'.bin'),b)
        self.store.append('flow_frame','f',dict(id='f',sha256=h,received_at=self.now))
        e=normalize_flow(data,self.now,self.c['universe'])[0];e.update(frame_id='f',raw_hash=h)
        self.store.append('flow_window','w',dict(id='w',started_at=self.now,completed_at=self.now,coverage='INCOMPLETE',events=[e]))
        self.assertTrue(audit_runtime(self.root)['passed'])
        row=self.store.db.execute("SELECT body FROM records WHERE kind='flow_window'").fetchone();body=json.loads(row[0]);body['events'][0]['quantity_base']=3
        for t, in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='records'").fetchall():self.store.db.execute('DROP TRIGGER '+t)
        encoded=canonical(body);self.store.db.execute("UPDATE records SET body=?,sha256=? WHERE kind='flow_window'",(encoded.decode(),hashlib.sha256(encoded).hexdigest()))
        with self.assertRaisesRegex(ValueError,'FLOW_UNITS'):audit_runtime(self.root)

    def test_complete_cash_case_matures_audits_and_becomes_real_origin_memory_only_in_fixture(self):
        # Entirely synthetic fixture with a genuinely elapsed short test deadline.
        # No fixture path or outcome is ever supplied to the production worker.
        import time
        c=dict(self.c,horizon_seconds=2,fit_hours=5,execution_delay_seconds=0,stream_seconds=.001)
        with patch('one_trading.alpha_loop.worker.defaults',return_value=c):initialize(self.root)
        p=self.store.get('alpha_protocol','frozen');loop=open_loop(self.root,c);self.addCleanup(loop.close)
        def receipt(data,url):
            key=digest([url,utc()]);raw=canonical(data);h=hashlib.sha256(raw).hexdigest();atomic_bytes(self.root/'raw'/(h+'.bin'),raw)
            return self.store.append('http_receipt',key,dict(id=key,url=url,sha256=h,requested_at=utc(),received_at=utc(),error=None))
        def flow(store,config,key):
            now=utc();data=dict(topic='allLiquidation.ETHUSDT',data=[dict(T=int(instant(now).timestamp()*1000),s='ETHUSDT',S='Buy',v='2',p='100')])
            raw=canonical(data);h=hashlib.sha256(raw).hexdigest();atomic_bytes(self.root/'raw'/(h+'.bin'),raw)
            fid=digest([key,now]);store.append('flow_frame',fid,dict(id=fid,sha256=h,received_at=now))
            e=normalize_flow(data,now,c['universe'])[0];e.update(frame_id=fid,raw_hash=h)
            return store.append('flow_window',key,dict(id=key,started_at=(instant(now)-timedelta(seconds=.001)).isoformat(),completed_at=now,events=[e],
                coverage='CONTIGUOUS_SUBSCRIBED_WINDOW',error=None,subscription_acknowledged=True,duplicate_frames=0,elapsed_seconds=.001,last_pong_at=now))
        def books():
            result={}
            for s in c['universe']:
                data=dict(bids=[['99.99','100000']],asks=[['100.01','100000']],lastUpdateId=1)
                r=receipt(data,'https://data-api.binance.vision/api/v3/depth?symbol='+s)
                b=self.book(100,r['received_at']);b['receipt_id']=r['id'];result[s]=b
            return result,{}
        def snapshot(store,config,key,flow):
            end=int(instant(flow['started_at']).timestamp()//3600)*3600000;n=c['fit_hours'];series={};rr={}
            for j,s in enumerate(c['universe']):
                series[s]=[[end-(n+1-i)*3600000,0,0,0,100+math.sin(i*(j+1))*.1,0,end-(n-i)*3600000-1] for i in range(n+1)]
                rr[s]=receipt(series[s],'https://data-api.binance.vision/api/v3/klines?symbol='+s)['id']
            fit=fit_exposures(series,c['universe'],end,n);bb,_=books();cutoff=utc()
            return store.append('alpha_snapshot',key,dict(id=key,cutoff=cutoff,universe=c['universe'],fit_end_ms=end,
                 fit_receipts=rr,fits=fit,books=bb,state=state_at(fit,bb,cutoff),errors={},flow_window_id=flow['id'],filters={s:self.filters for s in c['universe']}))
        def model(store,config,key,payload,**kwargs):
            folder=self.root/'models'/key;folder.mkdir(parents=True)
            a=dict(action='NO_TRADE',symbol='CASH',classification='UNKNOWN',p_reversion=.4,observed='synthetic',source_claim='synthetic',assumptions='a',counter_hypothesis='c',invalidation='i',rationale='r')
            atomic_bytes(folder/'input.json',canonical(payload));atomic_bytes(folder/'answer.json',canonical(a));atomic_bytes(folder/'prompt.txt',b'synthetic')
            return store.append('model_call',key,dict(id=key,status='CONNECTED_VALID',answer=a,input_hash=digest(payload),
                  prompt_hash=hashlib.sha256(b'synthetic').hexdigest(),response_hash=digest(a),artifact_directory=str(folder.relative_to(self.root)),started_at=utc(),completed_at=utc()))
        with patch('one_trading.alpha_loop.worker.capture_flow',side_effect=flow),patch('one_trading.alpha_loop.worker.collect_snapshot',side_effect=snapshot),patch('one_trading.alpha_loop.worker.books_now',side_effect=lambda *a:books()),patch('one_trading.alpha_loop.trading.books_now',side_effect=lambda *a:books()),patch('one_trading.alpha_loop.decisions.invoke',side_effect=model):
            new_case(self.store,loop,p,'case')
            case=self.store.get('alpha_case','case');time.sleep(max(0,(instant(case['due_at'])-instant(utc())).total_seconds())+.02)
            result=resolve_case(self.store,c,case)
            self.assertEqual(result['native']['CF_SEMANTIC']['metrics']['information_cost'],.00001)
            self.assertIsNone(result['native']['CF_SEMANTIC_MEMORY']['metrics'])
            self.assertTrue(audit_runtime(self.root)['passed'])
            self.assertEqual(len(retrieve_memory(self.store,utc())),1)
            scored=submit(self.store,loop);self.assertEqual(scored['semantic']['scored_clusters'],1)
            self.assertNotIn('memory',scored)
            self.assertFalse(scored['semantic']['edge_proven'])
            # Rehashing a wrong fee or economic label must not fool the independent auditor.
            body=self.store.get('alpha_outcome','case');body['native']['CF_SEMANTIC']['metrics']['information_cost']=0
            for name, in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='records'").fetchall():self.store.db.execute('DROP TRIGGER '+name)
            encoded=canonical(body);self.store.db.execute("UPDATE records SET body=?,sha256=? WHERE kind='alpha_outcome'",(encoded.decode(),hashlib.sha256(encoded).hexdigest()))
            with self.assertRaisesRegex(ValueError,'OUTCOME_information_cost'):audit_runtime(self.root)

    def test_quiet_or_incomplete_window_does_not_allocate_alpha_or_formal_case(self):
        initialize(self.root);p=self.store.get('alpha_protocol','frozen');loop=open_loop(self.root,self.c);self.addCleanup(loop.close)
        s=self.snapshot();f=dict(id='flow',events=[],coverage='CONTIGUOUS_SUBSCRIBED_WINDOW')
        with patch('one_trading.alpha_loop.worker.capture_flow',return_value=f),patch('one_trading.alpha_loop.worker.collect_snapshot',return_value=s):
            r=new_case(self.store,loop,p,'none')
        self.assertFalse(r['formal_case_registered']);self.assertEqual(len(self.store.all('alpha_case')),0)
        self.assertEqual(loop.db.execute('select count(*) from alpha_registry').fetchone()[0],0)
        self.assertEqual(len(self.store.all('alpha_numeric_screen')),1)

    def test_new_code_cohort_uses_same_authority_without_refunding_alpha(self):
        from one_trading.alpha_loop.reference.research_loop import ExperimentSpec
        initialize(self.root);loop=open_loop(self.root,self.c);self.addCleanup(loop.close)
        now=utc();deadline=(instant(now)+timedelta(days=1)).isoformat()
        loop.register_experiment(ExperimentSpec('old','old','CASHFLOW',1,now,deadline,0,1000,'FORWARD_PAPER'))
        loop.record_data('old',digest('observed'));self.assertEqual(loop.begin_forward('old')['j'],1)
        second=self.root/'next';p=initialize(second,self.root);other=open_loop(second,self.c);self.addCleanup(other.close)
        self.assertEqual(p['authority_database'],str(self.root/'authority.sqlite3'))
        now=utc();other.register_experiment(ExperimentSpec('next','next','CASHFLOW',1,now,deadline,0,1000,'FORWARD_PAPER'))
        other.record_data('next',digest('newly observed'));self.assertEqual(other.begin_forward('next')['j'],2)
        self.assertEqual(other.status('old')['alpha']['j'],1)

if __name__=='__main__':unittest.main()
