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
            max_output_tokens=1024,model='synthetic-model')
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

    def test_full_local_transport_common_epochs_and_independent_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            registry={k:dict(version=v,model=None,prompt_sha256=None,call_limit=0,cost_limit_usdt='0')
                      for k,v in [('CASH','cash-v1'),('PRICE_ONLY','momentum-v1')]}
            registry['AI']=dict(version='live-v1',model='free-probe-not-ai',prompt_sha256='b'*64,
                                call_limit=2,cost_limit_usdt='0')
            rec=EvidenceRecorder(root/'decisions.jsonl',run_id='fixture',source_class='SYNTHETIC',
                collector_commit='a'*40,registry=registry,start_at=BASE,model_calls_authorized=True)
            cfg=dict(max_calls=2,decision_interval_seconds=10,duration_seconds=90,cost_limit_usdt='0',source_commit='a'*40)
            planner=LivePlanner(rec,FreeProbe(),root,100,cfg)
            e=Experiment(Protocol(),{'CASH':ScheduledControl(CashPlanner(),planner),
                'PRICE_ONLY':ScheduledControl(MomentumPlanner(),planner),'AI':planner},data_use='SYNTHETIC')
            for i in range(5):
                f=packet(i,100+i,second_step=5)
                t=BASE+timedelta(seconds=i*5+.1)
                rec.freeze(f,source_receipts={'BTCUSDT':{'seq':i+1,'sha256':'c'*64,'received_at':f['received_at']}},
                           raw_cutoff_seq=i+1,memory=[],at=t,elapsed=i*5+.1)
                with patch('one_trading.ai_native.prospective_live.now',return_value=t), \
                     patch('one_trading.ai_native.prospective_live.time.monotonic',return_value=100+i*5+.25):
                    planner.select_epoch(f);e.observe(f)
            rec.close(at=BASE+timedelta(seconds=30),elapsed=30)
            paper=e.finish()
            write_new(root/'paper.json',paper)
            from one_trading.ai_native.engine import canonical
            (root/'paper-events.jsonl').write_bytes(b''.join(canonical(r)+b'\n' for r in paper['ledger']))
            write_new(root/'config.json',dict(cfg,mode='FREE_PROBE_NOT_AI'))
            write_new(root/'termination.json',dict(error=None,collector_returncode=0,model_uncertain=False))
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
