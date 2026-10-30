"""Clock, receipt, risk and transport boundary controls. All observations synthetic."""
import asyncio
import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.error import URLError
from one_sentinel.engine import Observer
from one_sentinel.demo import T,SYMBOL,metadata,mini,bar,book,sequence
from one_sentinel.clocks import calibration,assess
from one_sentinel.transport import public_get,validate_approval,recover_deliveries,smoke
from one_sentinel.store import raw_usage

def stamp(w,m,s='fixture'):
    return dict(wall=w,monotonic=m,session_id=s)

class Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'r';self.o=Observer(self.root)
    def tearDown(self):self.o.close();self.tmp.cleanup()
    def put(self,s,t,d,**kw):return self.o.ingest(s,json.dumps(d).encode(),t,t,**kw)
    def opened(self):
        for s,t,d in sequence()[:5]:self.put(s,t,d)
    def approve(self):
        c=self.o.l.get('contract','v1');a={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        a.update(approval_reference='SYNTHETIC_NO_IO',public_data=True,start_at=T-1,stop_at=T+1000,model_cash_budget=0,live_orders=False)
        validate_approval(self.o,a,T);return a
    def test_calibrated_items_and_bar_preserve_original_time(self):
        self.put('metadata',T,metadata('TRADING'))
        self.put('clock',T+.1,dict(serverTime=int((T+.65)*1000)),timing=stamp(T+.1,10.1),context=dict(request_timing=stamp(T,10)))
        data=mini(T+.5)+[dict(mini(T+.8)[0],s='UNCERTAINUSDT'),dict(mini(T+100)[0],s='FUTUREUSDT')]
        r=self.put('mini',T+.2,data,timing=stamp(T+.2,10.2))
        self.assertEqual(list(self.o.l.get('input',r['id'])['parsed']),[SYMBOL])
        self.assertEqual([x['state'] for x in self.o.l.get('interpretation',r['id'])['items']],['VALID','UNCERTAIN','FUTURE'])
        self.assertEqual(self.o.l.get('receipt',r['id'])['received_at'],T+.2)
        b=bar(T+.5);b['s']='BTCUSDT';r=self.put('bar',T+.3,b,timing=stamp(T+.3,10.3))
        self.assertIsNotNone(self.o.l.get('input',r['id']))
    def test_processing_delay_is_charged_before_feature_use(self):
        from one_sentinel.clocks import fresh
        self.approve()
        with patch('one_sentinel.engine.time.time',return_value=T+.1):
            self.put('clock',T+.1,dict(serverTime=int((T+.65)*1000)),classification='REAL_FORWARD',timing=stamp(T+.1,10.1),context=dict(request_timing=stamp(T,10)))
        late=stamp(T+20,30)
        with patch('one_sentinel.engine.time.time',return_value=T+.2),patch('one_sentinel.engine.capture',return_value=late):
            r=self.put('mini',T+.2,mini(T+.5),classification='REAL_FORWARD',timing=stamp(T+.2,10.2))
        value=self.o.l.get('input',r['id'])
        self.assertEqual(value['received_at'],T+.2);self.assertEqual(value['available_at'],T+20)
        self.assertFalse(fresh(value,'mini',SYMBOL,T+20,15,late))
    def test_sample_cannot_be_used_before_it_was_received_or_after_ttl(self):
        cfg=self.o.policy['clock_model'];c=calibration(int(T*1000),stamp(T,10),stamp(T+.1,10.1),'r',cfg)
        self.assertEqual(assess(T-10,stamp(T-.1,9.9),c,cfg)['state'],'UNVERIFIABLE')
        self.assertEqual(assess(T,stamp(T+200,210),c,cfg)['reason'],'CALIBRATION_EXPIRED')
    def test_clock_sample_rtt_and_wall_jump_rejected(self):
        cfg=self.o.policy['clock_model']
        for end in (stamp(T+3,13),stamp(T-100,10.1)):
            with self.assertRaises(ValueError):calibration(int(T*1000),stamp(T,10),end,'r',cfg)
    def test_malformed_item_and_duplicate_do_not_poison_independent_symbol(self):
        data=mini(T)+[dict(mini(T)[0],s='BADUSDT',c='NaN'),dict(mini(T)[0],s='DUPUSDT'),dict(mini(T)[0],s='DUPUSDT')]
        r=self.put('mini',T,data);self.assertEqual(list(self.o.l.get('input',r['id'])['parsed']),[SYMBOL])
        self.assertEqual(sum(x['state']=='INVALID' for x in self.o.l.get('interpretation',r['id'])['items']),3)
    def test_envelope_error_is_not_an_independent_valid_symbol(self):
        r=self.put('mini',T,dict(unexpected='object'));self.assertIsNone(self.o.l.get('input',r['id']))
    def test_non_scalar_symbol_is_isolated_and_independently_verifiable(self):
        import shutil,subprocess,sys
        values=mini(T)+[{},42,dict(mini(T)[0],s=['BROKEN'])]
        r=self.put('mini',T,values)
        self.assertEqual(list(self.o.l.get('input',r['id'])['parsed']),[SYMBOL])
        self.assertEqual([i['state'] for i in self.o.l.get('interpretation',r['id'])['items']],['VALID','INVALID','INVALID','INVALID'])
        dest=self.root/'export';self.o.export(dest);shutil.copytree(self.root/'raw',dest/'raw')
        verifier=Path(__file__).parents[1]/'scripts/verify_sentinel_corrections.py'
        run=subprocess.run([sys.executable,'-B',str(verifier),str(dest)],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
    def test_delayed_source_item_does_not_replace_newer_symbol(self):
        old=self.put('mini',T,mini(T))
        r=self.put('mini',T+1,mini(T-.5)+[dict(mini(T+1)[0],s='GOODUSDT')])
        self.assertEqual(list(self.o.l.get('input',r['id'])['parsed']),['GOODUSDT'])
        self.assertEqual(self.o.l.latest_input('mini',SYMBOL)['id'],old['id'])
    def test_uncertain_new_input_freezes_buy_and_keeps_open_inventory(self):
        self.opened();qty=self.o.status()['positions'][SYMBOL]['qty']
        self.put('mini',T+6,mini(T+100,'200')+[dict(mini(T+6)[0],s='GOODUSDT')])
        self.assertEqual(self.o.l.all('decision')[-1]['reason'],'DATA_GAP_RETAIN_INVENTORY')
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'],qty)
    def test_reduce_fill_then_exit_uses_only_residual_inventory(self):
        self.opened();self.put('mini',T+6,mini(T+6,'96'));self.put('book',T+8,book('96'))
        remaining=self.o.status()['positions'][SYMBOL]['qty']
        self.put('mini',T+9,mini(T+9,'94'));self.put('book',T+11,book('94'))
        self.assertEqual(self.o.l.all('fill')[-1]['qty'],remaining)
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'],'0.000')
        self.put('book',T+12,book('94'));self.assertEqual(len(self.o.l.all('fill')),3)
    def test_local_ack_recovery_needs_neither_approval_nor_url(self):
        with self.o.l.transaction():self.o.l.add('provider_receipt','ack',dict(id='ack',ack_at=T,phone_received_at=None,actual_delivery=False))
        recover_deliveries(self.o.l);recover_deliveries(self.o.l)
        self.assertEqual(len(self.o.l.all('delivery')),1);self.assertIsNone(self.o.l.all('delivery')[0]['phone_received_at'])
    def test_confirmed_connection_failure_charges_no_body(self):
        self.approve()
        class Fake:
            def open(self,*args,**kw):raise URLError(ConnectionRefusedError('refused'))
        with self.assertRaises(URLError):public_get(self.o,'metadata',clock=lambda:T,opener=Fake())
        r=self.o.l.all('http_result')[0];self.assertEqual(r['upper_bound_charge'],0);self.assertEqual(raw_usage(self.o.l,T),0)
    def test_crash_after_complete_receipt_does_not_charge_reservation_twice(self):
        self.approve();raw=json.dumps(metadata()).encode()
        with self.o.l.transaction():self.o.l.add('http_intent','pending',dict(id='pending',at=T,source='metadata',read_reservation=20000))
        self.o.ingest('metadata',raw,T,T,'REAL_FORWARD',context=dict(http_intent_id='pending'))
        self.o.close();self.o=Observer(self.root)
        self.assertEqual(raw_usage(self.o.l,T),len(raw))
    def test_byte_meter_rolls_off_without_releasing_uncertain_early(self):
        with self.o.l.transaction():
            self.o.l.add('http_intent','a',dict(id='a',at=T,source='metadata',read_reservation=100))
            self.o.l.add('http_result','a',dict(id='a',received_at=T+1,bytes=12,upper_bound_charge=100))
        self.assertEqual(raw_usage(self.o.l,T+86400),100)
        self.assertEqual(raw_usage(self.o.l,T+86402),0)
        self.assertEqual(raw_usage(self.o.l,T+2),100)
    def test_transport_tick_follows_normalization_availability(self):
        import time
        now=time.time();c=self.o.l.get('contract','v1')
        a={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        a.update(approval_reference='SYNTHETIC',public_data=True,start_at=now-1,stop_at=now+.2,model_cash_budget=0,live_orders=False)
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send(self,*args):pass
            async def recv(self):
                await asyncio.sleep(.01)
                return json.dumps(dict(stream='!miniTicker@arr',data=mini(time.time()-.1)))
        lags=[];original_tick=self.o.tick
        def tick(at,timing=None):
            revision=self.o.l.last('interpretation')
            if revision:lags.append(at-revision['available_at'])
            return original_tick(at,timing)
        with patch('one_sentinel.transport._public_thread'),patch.object(self.o,'tick',side_effect=tick):
            asyncio.run(smoke(self.o,a,.2,connector=Mock(return_value=WS())))
        self.assertTrue(lags)
        self.assertTrue(all(lag>=0 for lag in lags),lags)

    def test_websocket_cap_halts_before_connect_without_retry(self):
        import time
        self.o.policy['proposed_new_trial_limits']['raw_bytes_per_day']=1
        c=self.o.l.get('contract','v1');a={k:c[k] for k in ('policy_hash','account_hash','source_hash')};now=time.time()
        a.update(approval_reference='SYNTHETIC',public_data=True,start_at=now-1,stop_at=now+.2,model_cash_budget=0,live_orders=False)
        connect=Mock(side_effect=AssertionError('NO_CONNECT'))
        with patch('one_sentinel.transport._public_thread') as request:asyncio.run(smoke(self.o,a,.2,connector=connect))
        self.assertEqual(connect.call_count,0);self.assertEqual(request.call_count,0)
        self.assertEqual(self.o.l.all('observation_halt')[-1]['reason'],'RAW_DAILY_LIMIT')

if __name__=='__main__':unittest.main()
