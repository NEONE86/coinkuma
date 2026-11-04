"""Offline wiring/semantic attacks, never claims of live AI alpha."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_ai_native import packet, BASE, rehash_ledger
from one_trading.ai_native.engine import Experiment, Protocol, ModelDecision
from one_trading.ai_native.planners import CashPlanner, plan
from one_trading.ai_native.audit import verify, AuditError
from one_trading.ai_native.live_model import usage_cost, ResponsesModel, FreeProbe
from one_trading.ai_native.prospective_live import Tail, run
from one_trading.ai_native.prospective_live import LivePlanner, ScheduledControl, write_new
from one_trading.ai_native.prospective_evidence import EvidenceRecorder
from one_trading.ai_native.planners import MomentumPlanner
from one_trading.ai_native.live_audit import verify as verify_live


class Sequence:
    def __init__(self, expiry=100, charge=2):
        self.expiry, self.charge = expiry, charge
    def propose(self, context):
        if context['frame_id']=='source-0':
            return ModelDecision(plan('BUY','BTCUSDT',.1,'fixture'), .5, self.charge,
                                 (BASE+timedelta(seconds=self.expiry)).isoformat())
        if context['frame_id']=='source-2':
            return ModelDecision(plan('SELL','BTCUSDT',1.,'stop loss / exit'), .5, self.charge)
        return None


class LiveTests(unittest.TestCase):
    def test_review_independent_approval_reconciliation(self):
        from one_trading.ai_native.live_audit import verify_approval
        from one_trading.ai_native.audit import digest
        a=dict(model_calls_authorized=True,approval_reference='fixture-approval',source_commit='a'*40,
            run_id='fixture',max_calls=2,cost_limit_usdt='.10',duration_seconds=90,max_bytes=1000000,
            decision_interval_seconds=15,model='fixture-model',reserve_per_call_usdt='.05',usd_usdt='1',
            expires_at='2099-01-01T00:00:00+00:00')
        c=dict(a,approval=a,prompt_sha256='b'*64)
        h=dict(collector_commit='a'*40,run_id='fixture',registry={'AI':dict(model='fixture-model',
            prompt_sha256='b'*64,call_limit=2,cost_limit_usdt='.10')})
        requests={'r':dict(at=BASE.isoformat(),payload=dict(model='fixture-model',reserve_cost_usdt='.05'))}
        def claim(approval):return dict(approval_sha256=digest(approval),run_id='fixture',source_commit='a'*40,
                                        claimed_at=(BASE-timedelta(seconds=1)).isoformat())
        verify_approval(c,h,requests,claim(a))
        for key,value in [('model_calls_authorized',False),('max_calls',0),('model','other'),
                          ('source_commit','f'*40),('expires_at','2000-01-01T00:00:00+00:00'),
                          ('cost_limit_usdt','0')]:
            with self.subTest(key=key):
                bad=copy.deepcopy(c);bad['approval'][key]=value
                with self.assertRaises(AuditError):verify_approval(bad,h,requests,claim(bad['approval']))

    def test_review_shortened_paper_protocol_cannot_pass_cross_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root,protocol=Protocol(execution_delay_seconds=0))
            # Producer and its own auditor agree on zero delay; cross-audit must not.
            verify(json.loads((root/'paper.json').read_text()))
            with self.assertRaisesRegex(AuditError,'UNPINNED_PAPER_PROTOCOL'):verify_live(root)

    def test_review_unknown_completion_cannot_become_free_failed_success(self):
        class Unknown(FreeProbe):
            def respond(self,c):raise TimeoutError('synthetic uncertainty')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root,model=Unknown())
            path=root/'decisions.jsonl'
            rows=[json.loads(x) for x in path.read_bytes().splitlines()]
            req=next(r for r in rows if r['kind']=='decision_requested')
            failed=copy.deepcopy(req);failed['kind']='decision_failed'
            failed['payload']=dict(request_id=req['payload']['request_id'],failure_reason='UNKNOWN_COMPLETION',
                                   actual_cost_usdt='0',input_tokens=0,output_tokens=0)
            rows.insert(rows.index(req)+1,failed)
            rows[-1]['payload']['unresolved_requests']=[]
            from test_prospective_evidence import rehash
            path.write_bytes(rehash(rows))
            r=verify_live(root)
            self.assertEqual(r['source_decision_audit']['unresolved_requests'],0)
            self.assertEqual(r['status'],'INCOMPLETE')

    def test_review_approval_claim_is_atomic_across_runtime_parents(self):
        from one_trading.ai_native.prospective_live import consume_approval
        from concurrent.futures import ThreadPoolExecutor
        approval=dict(approval_reference='same-approval',run_id='same-basename',source_commit='a'*40)
        def claim(_):
            try:consume_approval(approval);return 'claimed'
            except FileExistsError:return 'refused'
        with tempfile.TemporaryDirectory() as d, patch('pathlib.Path.home',return_value=Path(d)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(claim,range(2))),['claimed','refused'])

    def test_review_expiry_checked_at_provider_dispatch(self):
        approval=dict(model_calls_authorized=True,approval_reference='synthetic',usd_usdt='1',
            rates={'input':'.75','cached':'.075','output':'4.5'},reserve_per_call_usdt='.05',
            max_output_tokens=1024,model='synthetic-model',expires_at='2000-01-01T00:00:00+00:00')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'synthetic-not-secret'}),patch('urllib.request.build_opener') as opener:
            model=ResponsesModel(approval)
            with self.assertRaisesRegex(ValueError,'APPROVAL_EXPIRED'):model.respond({})
            opener.assert_not_called()

    def test_model_cost_not_backdated_to_input_frame(self):
        class Slow:
            def propose(self,c):
                if c['frame_id']=='source-0':
                    return ModelDecision(plan('BUY','BTCUSDT',.1,'slow fixture'),10,2)
                return None
        e=Experiment(Protocol(),{'CASH':CashPlanner(),'AI':Slow()},data_use='SYNTHETIC')
        for i in range(4): e.observe(packet(i,100,second_step=5))
        r=e.finish();verify(r)
        charge=next(x['payload'] for x in r['ledger'] if x['kind']=='model_cost')
        self.assertEqual(charge['frame_id'],'source-2')
        fill=next(x['payload'] for x in r['ledger'] if x['kind']=='paper_fill')
        self.assertEqual(fill['quote_frame'],'source-3')

    def test_mock_provider_preserves_original_usage_and_response(self):
        import io
        from unittest.mock import MagicMock
        approval=dict(model_calls_authorized=True,approval_reference='synthetic',usd_usdt='1',
            rates={'input':'.75','cached':'.075','output':'4.5'},reserve_per_call_usdt='.05',
            max_output_tokens=1024,model='synthetic-model',expires_at='2099-01-01T00:00:00+00:00')
        answer=plan('HOLD','CASH',0.,'fixture')
        envelope={'status':'completed','usage':{'input_tokens':100,'input_tokens_details':{'cached_tokens':0},'output_tokens':20},
                  'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer)}]}]}
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps(envelope).encode()
        opener=MagicMock();opener.open.return_value=response
        with patch.dict('os.environ',{'OPENAI_API_KEY':'synthetic-not-secret'}), \
             patch('urllib.request.build_opener',return_value=opener):
            model=ResponsesModel(approval);raw,receipt=model.respond({'frame_id':'fixture'})
        self.assertEqual(json.loads(raw),answer)
        self.assertEqual(receipt['cost'],'0.00016500')
        self.assertEqual(json.loads(receipt['provider_raw_utf8']),envelope)
        self.assertEqual(opener.open.call_count,1)

    def fixture(self, root, model=None, protocol=None):
        registry={k:dict(version=v,model=None,prompt_sha256=None,call_limit=0,cost_limit_usdt='0')
                  for k,v in [('CASH','cash-v1'),('PRICE_ONLY','momentum-v1')]}
        registry['AI']=dict(version='live-v1',model='free-probe-not-ai',prompt_sha256='b'*64,
                            call_limit=2,cost_limit_usdt='0')
        rec=EvidenceRecorder(root/'decisions.jsonl',run_id='fixture',source_class='SYNTHETIC',
            collector_commit='a'*40,registry=registry,start_at=BASE,model_calls_authorized=True)
        cfg=dict(max_calls=2,decision_interval_seconds=10,duration_seconds=90,cost_limit_usdt='0',source_commit='a'*40)
        planner=LivePlanner(rec,model or FreeProbe(),root,100,cfg)
        e=Experiment(protocol or Protocol(),{'CASH':ScheduledControl(CashPlanner(),planner),
            'PRICE_ONLY':ScheduledControl(MomentumPlanner(),planner),'AI':planner},data_use='SYNTHETIC')
        for i in range(5):
            f=packet(i,100+i,second_step=5)
            t=BASE+timedelta(seconds=i*5+.1)
            rec.freeze(f,source_receipts={'BTCUSDT':{'seq':i+1,'sha256':'c'*64,'received_at':f['received_at']}},
                       raw_cutoff_seq=i+1,memory=[],at=t,elapsed=i*5+.1)
            with patch('one_trading.ai_native.prospective_live.now',return_value=t), \
                 patch('one_trading.ai_native.prospective_live.time.monotonic',return_value=100+i*5+.25):
                planner.select_epoch(f);e.observe(f,source_received_at={'BTCUSDT':f['received_at']})
        rec.close(at=BASE+timedelta(seconds=30),elapsed=30)
        paper=e.finish()
        write_new(root/'paper.json',paper)
        from one_trading.ai_native.engine import canonical
        (root/'paper-events.jsonl').write_bytes(b''.join(canonical(r)+b'\n' for r in paper['ledger']))
        write_new(root/'config.json',dict(cfg,mode='FREE_PROBE_NOT_AI'))
        write_new(root/'termination.json',dict(error=None,collector_returncode=0,model_uncertain=False))

    def test_full_local_transport_common_epochs_and_independent_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.fixture(root)
            r=verify_live(root)
            self.assertEqual(r['status'],'PASS_CONNECTION_ONLY')
            self.assertEqual(r['summary']['AI']['fills'],2)
            self.assertEqual(r['summary']['AI']['open_positions'],0)
            self.assertEqual({r['summary'][a]['decisions'] for a in r['summary']},{2})
            # Changing original full input cannot be concealed by valid PAPER accounting.
            inp=root/'request-1-input.json';c=json.loads(inp.read_text());c['cash']=9000
            inp.write_text(json.dumps(c))
            with self.assertRaisesRegex(AuditError,'CONTEXT'):verify_live(root)

    def result(self, expiry=100, charge=2):
        e=Experiment(Protocol(), {'CASH': CashPlanner(),'AI': Sequence(expiry,charge)}, data_use='SYNTHETIC')
        for i, price in enumerate((100,101,99,98)):
            e.observe(packet(i,price,second_step=5))
        return e.finish()

    def test_cost_debit_sizes_next_fill_and_sell_exit(self):
        r=self.result(); verify(r)
        fills=[x['payload'] for x in r['ledger'] if x['kind']=='paper_fill']
        self.assertEqual(len(fills),2)
        self.assertAlmostEqual(fills[0]['quantity']*fills[0]['execution_price'],999.8)
        self.assertEqual(r['summary']['AI']['model_costs_paid'],4)
        self.assertEqual(r['summary']['AI']['open_positions'],0)
        self.assertEqual(r['summary']['CASH']['pnl_marked_at_bid'],0)
        self.assertLess(r['summary']['AI']['pnl_marked_at_bid'],0)

    def test_new_sample_of_old_quote_cannot_fill(self):
        e=Experiment(Protocol(),{'CASH':CashPlanner(),'AI':Sequence()},data_use='SYNTHETIC')
        e.observe(packet(0,100,second_step=5),source_received_at={'BTCUSDT':BASE.isoformat()})
        e.observe(packet(1,100,second_step=5),source_received_at={'BTCUSDT':BASE.isoformat()})
        r=e.finish();verify(r)
        self.assertEqual(r['summary']['AI']['fills'],0)
        self.assertEqual(next(x['payload']['reason'] for x in r['ledger'] if x['kind']=='order_rejected'),
                         'PRE_DECISION_SOURCE_QUOTE')

    def test_expired_proposal_never_fills(self):
        r=self.result(expiry=3); verify(r)
        self.assertEqual(r['summary']['AI']['fills'],0)
        self.assertIn('PROPOSAL_EXPIRED',[x['payload']['reason'] for x in r['ledger'] if x['kind']=='order_rejected'])

    def test_rehashed_cost_tamper_rejected(self):
        r=self.result()
        next(x for x in r['ledger'] if x['kind']=='model_cost')['payload']['amount']=0
        rehash_ledger(r)
        with self.assertRaises(AuditError): verify(r)

    def test_rehashed_context_tamper_rejected(self):
        r=self.result()
        next(x for x in r['ledger'] if x['kind']=='decision')['payload']['context_hash']='0'*64
        rehash_ledger(r)
        with self.assertRaisesRegex(AuditError,'CONTEXT'): verify(r)

    def test_rehashed_order_expiry_tamper_rejected(self):
        r=self.result()
        next(x for x in r['ledger'] if x['kind']=='order_intent')['payload']['valid_until']=(BASE+timedelta(days=1)).isoformat()
        rehash_ledger(r)
        with self.assertRaisesRegex(AuditError,'EXPIRY'): verify(r)

    def test_cost_without_valid_answer_is_still_debited(self):
        class Failed:
            def propose(self, c): return ModelDecision(None,1,3)
        e=Experiment(Protocol(),{'CASH':CashPlanner(),'AI':Failed()},data_use='SYNTHETIC')
        e.observe(packet(0));e.observe(packet(1));r=e.finish();verify(r)
        self.assertEqual(r['summary']['AI']['cash'],9997)
        self.assertEqual(r['summary']['AI']['pending_model_cost_usdt'],3)
        self.assertEqual(r['summary']['AI']['model_unavailable'],2)

    def test_usage_pricing_cached_tokens_and_round_up(self):
        self.assertEqual(usage_cost({'input_tokens':100,'input_tokens_details':{'cached_tokens':20},'output_tokens':10},
                                   {'input':'1','cached':'.5','output':'2'}),'0.00011000')

    def test_bad_usage_rejected(self):
        with self.assertRaises(ValueError):
            usage_cost({'input_tokens':True,'input_tokens_details':{'cached_tokens':0},'output_tokens':1},
                       {'input':'1','cached':'1','output':'1'})

    def test_no_implicit_model_permission(self):
        with self.assertRaisesRegex(ValueError,'APPROVAL'):
            ResponsesModel({})

    def test_partial_tail_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x';p.write_bytes(b'{"a":1}\n{"b":')
            t=Tail(p);self.assertEqual(t.read(),[{'a':1}]);self.assertEqual(t.read(),[])
            with p.open('ab') as f:f.write(b'2}\n')
            self.assertEqual(t.read(),[{'b':2}])

    def test_probe_explicit_non_ai(self):
        answer,receipt=FreeProbe().respond({'positions':{},'pending_order':False})
        self.assertEqual(receipt['kind'],'FREE_PROBE_NOT_AI')
        self.assertEqual(json.loads(answer)['action'],'BUY')

    def test_no_network_without_flag(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'NETWORK'):
                run(Path(d)/'new',{})

    def test_existing_runtime_never_reused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'NEW_PRIVATE'):
                run(Path(d),{},enable_public_network=True)
