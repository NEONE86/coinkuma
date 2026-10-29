"""Event protocol regressions; all fixtures are synthetic, never market evidence."""
import copy,importlib.util,tempfile,unittest
from pathlib import Path
import test_batch_transitions as fixtures
from one_trading.autonomy.store import Store,utc,digest
from one_trading.hypothesis_loop import propose,evaluate as evaluate_grid,run_queue,ABSORPTION,DELAY
from one_trading.event_scan import register,evaluate,successor,contexts,PROTOCOL

def auditor():
    p=Path(__file__).parents[1]/'scripts/verify_research_transitions.py';sp=importlib.util.spec_from_file_location('coverage_audit',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m

class EventTests(unittest.TestCase):
    def fixture(self,root):
        alpha,batch,loop=fixtures.TransitionTests().fixture(root);s=Store(batch);data=fixtures.TransitionTests().seed(s)
        old=propose(s,'parent');r=evaluate_grid(s,old);s.append('hypothesis_trial',old['id'],dict(id=old['id'],request_id='legacy',parent_trial_id='parent',source_version=old['source_version'],completed_at=utc(),**r))
        return s,loop,data,old
    def test_event_recompute_all_clocks_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,loop,data,old=self.fixture(Path(tmp))
            try:
                spec=register(s,old['id']);r=dict(id=spec['id'],completed_at=utc(),**evaluate(s,spec));self.assertEqual(r['status'],'NO_ELIGIBLE_EVENTS')
                self.assertGreater(len(r['contexts']),700);self.assertEqual(len(r['rows']),4*len(r['contexts']))
                export=Path(tmp)/'export';dest=export/'raw/batch';dest.mkdir(parents=True)
                for p in (s.root/'raw').glob('*.bin'):(dest/p.name).write_bytes(p.read_bytes())
                audit=auditor();audit.recompute(export,spec,r)
                for change in ('quantity','cost','status','overlap','clock','feature','ai'):
                    bad=copy.deepcopy(r)
                    if change=='quantity':bad['rows'][0]['quantity']+=1
                    elif change=='cost':bad['rows'][0]['fee_usdt']+=1
                    elif change=='status':bad['status']='POSITIVE_EXPLORATORY'
                    elif change=='overlap':bad['overlap_exclusions']=[]
                    elif change=='clock':bad['contexts'].pop()
                    elif change=='feature':bad['contexts'][0]['assets']['BTCUSDT']['volume_ratio']+=1
                    else:bad['rows'][3]['net_usdt']=0
                    with self.assertRaises(AssertionError,msg=change):audit.recompute(export,spec,bad)
            finally:s.close();loop.close()
    def test_economic_entries_overlap_costs_and_bad_future_inputs(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            s,l,data,old=self.fixture(Path(tmp))
            try:
                spec=register(s,old['id']);cs=contexts(data)[0];times=[cs[i]['signal_ms'] for i in (10,12,40)]
                for bar in data['BTCUSDT']['bars']:
                    if bar[0] in [t-3600000 for t in times]:bar[1]=bar[4]=99;bar[7]=400;bar[10]=40
                with patch('one_trading.event_scan.read_inputs',return_value=data):r=evaluate(s,spec)
                eco=[x for x in r['rows'] if x['arm']=='ECONOMIC_RULE' and x['symbol']]
                self.assertEqual(len(r['eligible_events']),3);self.assertEqual(len(eco),2)
                self.assertEqual([x['signal_ms'] for x in eco],[times[0],times[2]])
                self.assertEqual(len([x for x in r['overlap_exclusions'] if x['arm']=='ECONOMIC_RULE']),1)
                for trade in eco:
                    self.assertEqual(trade['entry_ms']-trade['signal_ms'],7200000)
                    self.assertAlmostEqual(trade['gross_usdt']-trade['trading_cost_usdt'],trade['net_usdt'])
                    self.assertLessEqual(trade['quantity']*trade['entry_price']+trade['quantity']*trade['entry_price']*.0016,1250.000001)
            finally:s.close();l.close()

    def test_completed_features_invariant_to_future_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,l,data,old=self.fixture(Path(tmp))
            try:
                before=contexts(data)[0][0];changed=copy.deepcopy(data);t=before['signal_ms']
                for d in changed.values():
                    for bar in d['bars']:
                        if bar[0]>=t:bar[1]=bar[4]=999;bar[7]=9999
                self.assertEqual(before,contexts(changed)[0][0])
            finally:s.close();l.close()
    def test_result_selects_different_bounded_actions_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,l,data,old=self.fixture(Path(tmp))
            try:
                base=register(s,old['id']);actions=[]
                for i,status in enumerate(('NO_ELIGIBLE_EVENTS','NEGATIVE_AFTER_COST','POSITIVE_EXPLORATORY','DATA_INSUFFICIENT','EXECUTION_ERROR','EXPIRED')):
                    key='synthetic-'+str(i);content={k:v for k,v in base.items() if k not in ('id','stored_at')};s.append('hypothesis_spec',key,dict(id=key,**content))
                    parent=s.append('hypothesis_trial',key,dict(id=key,status=status,source_receipts=base['source_receipts'],mechanism_id=ABSORPTION,completed_at=utc(),error='synthetic missing' if status in ('DATA_INSUFFICIENT','EXECUTION_ERROR') else None,counts={}))
                    a=successor(s,parent);b=successor(s,parent);self.assertEqual(a,b);actions.append(a['action'])
                    self.assertEqual(a['resource_limits']['model_calls'],0);self.assertEqual(a['selection_rule'],base['selection_rule']);self.assertTrue(a['falsification'])
                self.assertEqual(len(set(actions)),6)
            finally:s.close();l.close()
    def test_request_restart_has_no_second_trial_or_charge(self):
        with tempfile.TemporaryDirectory() as tmp:
            s,l,data,old=self.fixture(Path(tmp))
            try:
                base=register(s,old['id']);root=s.root;s.close()
                r=run_queue(root,'event-test',1);spent=l.engine_status('CASHFLOW')['time_spent'];again=run_queue(root,'event-test',1)
                self.assertEqual(again['status'],'CACHED');self.assertEqual(r['counts']['historical_hypothesis_trials'],1);self.assertEqual(l.engine_status('CASHFLOW')['time_spent'],spent)
            finally:s.close();l.close()

class CoverageTests(unittest.TestCase):
    def fixture(self,status):
        spec=dict(id='x',parent_trial_id='p',source_version='v',expiry='2026-10-29T00:00:00+00:00',source_receipts={});spec['spec_hash']=digest({k:v for k,v in spec.items() if k!='id'})
        body=dict(status=status,source_receipts={},counts={},mechanism_id=ABSORPTION,error='bounded failure')
        trial=dict(id='x',request_id='q',parent_trial_id='p',source_version='v',completed_at='2026-10-29T01:00:00+00:00',**body)
        receipt=dict(id='x',request_id='q',input_hash=spec['spec_hash'],output_hash=digest(body),result=body)
        a=auditor();end=dict(id='q',trial_ids=['x'],statuses={status:1},counts=dict.fromkeys(a.COUNT_KEYS,0))
        return a,{'x':spec},[trial],[receipt],[dict(id='q')],[end]
    def test_real_errors_remain_audited_noncomputable(self):
        for status in ('EXECUTION_ERROR','DATA_INSUFFICIENT','EXPIRED'):
            a,*args=self.fixture(status);r,v=a.audit_coverage(*args,lambda s,t:None)
            self.assertEqual(r['noncomputable_trial_ids'],['x']);self.assertEqual(len(r['trials']),1);self.assertEqual(v,{})
    def test_error_payload_cannot_hide_financial_results_even_with_matching_receipt(self):
        a,s,t,r,q,e=self.fixture('EXECUTION_ERROR');t[0]['aggregates']={'ECONOMIC_RULE':{'net_usdt':1000000}};r[0]['result']['aggregates']=t[0]['aggregates'];r[0]['output_hash']=digest(r[0]['result'])
        with self.assertRaises(AssertionError):a.audit_coverage(s,t,r,q,e,lambda s,t:None)
    def test_request_status_and_missing_trial_rejected(self):
        a,s,t,r,q,e=self.fixture('EXECUTION_ERROR');e[0]['statuses']={'POSITIVE_EXPLORATORY':1}
        with self.assertRaises(AssertionError):a.audit_coverage(s,t,r,q,e,lambda s,t:None)
        with self.assertRaises(AssertionError):a.audit_coverage(s,[],r,q,e,lambda s,t:None)
    def test_receipt_status_must_match_trial(self):
        a,s,t,r,q,e=self.fixture('EXECUTION_ERROR');r[0]['result']=dict(r[0]['result'],status='POSITIVE_EXPLORATORY');r[0]['output_hash']=digest(r[0]['result'])
        with self.assertRaises(AssertionError):a.audit_coverage(s,t,r,q,e,lambda s,t:None)

if __name__=='__main__':unittest.main()
