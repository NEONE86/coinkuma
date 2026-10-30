"""Offline audit counterexamples: bounded release and actual epoch changes."""
import json
import tempfile
import unittest
from pathlib import Path
from one_sentinel.engine import Observer
from one_sentinel.demo import T,SYMBOL,metadata,mini,bar,book,sequence

def stamp(at,session='A'):
    return dict(wall=at,monotonic=10+(at-T) if session=='A' else 1+(at-T-7),session_id=session)

def put(o,source,at,payload,**kw):
    return o.ingest(source,json.dumps(payload).encode(),at,at,**kw)

def uncertain(o):
    put(o,'metadata',T,metadata('BREAK'))
    put(o,'clock',T+.6,dict(serverTime=int((T+.3)*1000)),timing=stamp(T+.6),context=dict(request_timing=stamp(T)))
    put(o,'metadata',T+.7,metadata('TRADING'),timing=stamp(T+.7))
    receipts=[put(o,'mini',at,mini(at-.01),timing=stamp(at)) for at in (T+1,T+1.1,T+1.2)]
    receipts.append(put(o,'bar',T+1.3,bar(T+1.29),timing=stamp(T+1.3)))
    return receipts

class PendingEpochTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'runtime';self.o=Observer(self.root)
    def tearDown(self):self.o.close();self.temp.cleanup()
    def calibrate(self):
        put(self.o,'clock',T+.6,dict(serverTime=int((T+.3)*1000)),timing=stamp(T+.6),context=dict(request_timing=stamp(T)))
    def test_600ms_rtt_10ms_ws_releases_after_availability_and_new_delay(self):
        receipts=uncertain(self.o)
        original=[self.o.l.get('interpretation',r['id']) for r in receipts]
        self.assertEqual([r['items'][0]['state'] for r in original],['UNCERTAIN']*4)
        self.assertEqual(sum(r['source'] in ('mini','bar') for r in self.o.l.all('input')),0)
        self.assertFalse(any(d['action']=='ENTER' for d in self.o.l.all('decision')))
        self.o.tick(T+2,stamp(T+2))
        self.assertEqual(sum(r['source'] in ('mini','bar') for r in self.o.l.all('input')),4)
        self.assertEqual([self.o.l.get('interpretation',r['id']) for r in receipts],original)
        entry=self.o.l.last('decision');self.assertEqual(entry['action'],'ENTER');self.assertGreaterEqual(entry['at'],T+2)
        put(self.o,'book',T+3.99,book(),timing=stamp(T+3.99));self.assertEqual(len(self.o.l.all('fill')),0)
        put(self.o,'book',T+4,book(),timing=stamp(T+4));self.assertEqual(len(self.o.l.all('fill')),1)
    def test_structural_exit_new_epoch_reissued_at_8_and_filled_once_at_10(self):
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        put(self.o,'metadata',T+6,metadata('BREAK'),timing=stamp(T+6))
        old=self.o.l.last('decision');alerts=len(self.o.l.all('outbox'))
        self.o.close();self.o=Observer(self.root)
        put(self.o,'book',T+8,book('94'),timing=stamp(T+8,'B'))
        new=self.o.l.last('decision')
        self.assertNotEqual(new['id'],old['id']);self.assertEqual(new['action'],'EXIT')
        self.assertEqual(self.o.l.get('execution',old['id'])['reason'],'NEW_MONOTONIC_EPOCH')
        self.assertEqual((new['at'],new['ready_at']),(T+8,T+10))
        for t in (9,10,10,20):put(self.o,'book',T+t,book('94'),timing=stamp(T+t,'B'))
        self.assertEqual([f['action'] for f in self.o.l.all('fill')],['ENTER','EXIT'])
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'],'0.000')
        self.assertEqual(len(self.o.l.all('outbox')),alerts)
        self.assertEqual(self.o.l.get('decision',old['id']),old)
    def test_new_calibration_releases_without_tick(self):
        uncertain(self.o)
        put(self.o,'clock',T+1.6,dict(serverTime=int((T+1.595)*1000)),timing=stamp(T+1.6),context=dict(request_timing=stamp(T+1.59)))
        self.assertEqual(len(self.o.l.all('interpretation_release')),4)
        self.assertEqual(self.o.l.last('decision')['action'],'ENTER')
        self.assertEqual(self.o.l.last('decision')['ready_at'],T+3.6)
    def test_partial_symbol_release_preserves_already_valid_peer(self):
        self.calibrate();values=[dict(mini(T+.9)[0],s='VALIDUSDT'),mini(T+1.29)[0]]
        r=put(self.o,'mini',T+1.3,values,timing=stamp(T+1.3));original=self.o.l.get('input',r['id'])
        self.assertEqual(list(original['parsed']),['VALIDUSDT'])
        self.o.tick(T+2,stamp(T+2));self.o.tick(T+2,stamp(T+2))
        duplicate=put(self.o,'mini',T+1.3,values,timing=stamp(T+1.3))
        self.assertTrue(duplicate['duplicate']);self.assertEqual(self.o.l.get('input',r['id']),original)
        self.assertEqual(self.o.l.latest_input('mini','VALIDUSDT')['id'],r['id'])
        self.assertEqual(len(self.o.l.all('input')),2);self.assertEqual(len(self.o.l.all('interpretation_release')),1)
    def test_no_head_of_line_blocking(self):
        self.calibrate()
        put(self.o,'mini',T+1.3,[dict(mini(T+1.55)[0],s='SLOWUSDT'),dict(mini(T+1.29)[0],s='FASTUSDT')],timing=stamp(T+1.3))
        self.o.tick(T+1.61,stamp(T+1.61))
        self.assertIsNone(self.o.l.latest_input('mini','SLOWUSDT'));self.assertIsNotNone(self.o.l.latest_input('mini','FASTUSDT'))
        self.assertEqual(self.o.status()['clock_pending']['active_items'],1)
    def test_ttl_and_calibration_expiry_are_distinct_terminal_outcomes(self):
        self.calibrate();put(self.o,'mini',T+1.3,mini(T+1.29),timing=stamp(T+1.3))
        self.o.tick(T+6.3,stamp(T+6.3));self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'PENDING_TTL')
        # A separate original still has a contemporary but almost expired sample.
        put(self.o,'mini',T+120,mini(T+119.99),timing=stamp(T+120))
        self.o.tick(T+121,stamp(T+121));self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'CALIBRATION_EXPIRED')
        self.assertEqual(len(self.o.l.all('interpretation_release')),0)
    def test_wall_jump_and_new_epoch_never_release(self):
        self.calibrate();put(self.o,'mini',T+1.3,mini(T+1.29),timing=stamp(T+1.3))
        self.o.tick(T+2,dict(wall=T+2,monotonic=11.5,session_id='A'))
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'CLOCK_WALL_JUMP')
        put(self.o,'mini',T+2.1,mini(T+2.09),timing=stamp(T+2.1))
        self.o.close();self.o=Observer(self.root);self.o.tick(T+2.5,stamp(T+2.5,'B'))
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'NEW_MONOTONIC_EPOCH')
        self.assertEqual(len(self.o.l.all('interpretation_release')),0)
    def test_stale_closed_bar_never_released(self):
        put(self.o,'metadata',T,metadata('BREAK'));self.calibrate();put(self.o,'metadata',T+.7,metadata('TRADING'))
        value=bar(T+1.29);value['k'].update(t=int((T-240)*1000),T=int((T-180)*1000)-1)
        put(self.o,'bar',T+1.3,value,timing=stamp(T+1.3));self.o.tick(T+2,stamp(T+2))
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'STALE_AT_RELEASE')
        self.assertIsNone(self.o.l.latest_input('bar',SYMBOL))
    def test_future_invalid_and_reused_never_join_queue(self):
        self.calibrate();put(self.o,'mini',T+1.3,mini(T+10)+[{}],timing=stamp(T+1.3))
        self.o.tick(T+20,stamp(T+20));self.assertEqual(self.o.l.all('clock_pending'),[])
        self.o.close();self.o=Observer(self.root.parent/'reused')
        put(self.o,'mini',T,mini(T-.1),classification='REAL_REUSED')
        self.o.tick(T+20,stamp(T+20));self.assertEqual(self.o.l.all('clock_pending'),[])
    def test_capacity_items_and_bytes_are_counted_without_unbounded_queue(self):
        self.calibrate();self.o.policy['clock_pending']['max_items']=1
        put(self.o,'mini',T+1.3,mini(T+1.29)+[dict(mini(T+1.29)[0],s='OTHERUSDT')],timing=stamp(T+1.3))
        self.assertEqual(self.o.status()['clock_pending']['active_items'],1)
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'CAPACITY_ITEMS')
        self.o.tick(T+2,stamp(T+2));self.o.policy['clock_pending']['max_envelope_bytes']=1
        put(self.o,'mini',T+2.1,mini(T+2.09),timing=stamp(T+2.1))
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'CAPACITY_BYTES')
        self.assertEqual(self.o.status()['clock_pending']['active_items'],0)
    def test_horizon_exit_reissued_with_new_epoch_and_own_delay(self):
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        at=T+5+self.o.policy['horizon_seconds'];self.o.tick(at,stamp(at));old=self.o.l.last('decision')
        self.o.close();self.o=Observer(self.root)
        put(self.o,'book',at+2,book(),timing=stamp(at+2,'B'))
        new=self.o.l.last('decision');self.assertEqual(new['reason'],'HORIZON_EXPIRED');self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(new['ready_at'],at+4)
        put(self.o,'book',at+3,book(),timing=stamp(at+3,'B'));self.assertEqual(len(self.o.l.all('fill')),1)
        put(self.o,'book',at+4,book(),timing=stamp(at+4,'B'));self.assertEqual(len(self.o.l.all('fill')),2)
    def test_stop_loss_without_current_epoch_price_is_not_reissued(self):
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        put(self.o,'clock',T+5.9,dict(serverTime=int((T+5.895)*1000)),timing=stamp(T+5.9),context=dict(request_timing=stamp(T+5.89)))
        put(self.o,'mini',T+6,mini(T+5.8,'94'),timing=stamp(T+6))
        old=self.o.l.last('decision')
        self.assertEqual(old['reason'],'STOP_LOSS');self.assertEqual(old['timing']['session_id'],'A')
        self.o.close();self.o=Observer(self.root)
        self.o.tick(T+8,stamp(T+8,'B'))
        self.assertEqual(self.o.l.get('execution',old['id'])['reason'],'NEW_MONOTONIC_EPOCH')
        self.assertEqual(self.o.l.last('decision')['reason'],'DATA_GAP_RETAIN_INVENTORY')
        self.assertEqual(self.o.l.all('decision_retry'),[])
    def test_epoch_retry_bound_and_no_hot_generation(self):
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        put(self.o,'metadata',T+6,metadata('BREAK'),timing=stamp(T+6))
        for i in range(1,6):
            at=T+6+i/10;s=dict(wall=at,monotonic=100+i,session_id=f'epoch-{i}')
            self.o.tick(at,s);self.o.tick(at,s)
        self.assertEqual(len(self.o.l.all('decision_retry')),3)
        self.assertTrue(self.o.l.all('retry_blocked'));self.assertEqual(len(self.o.l.all('fill')),1)
    def test_non_epoch_rejection_has_no_retry_generation(self):
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        put(self.o,'metadata',T+6,metadata('BREAK'),timing=stamp(T+6))
        wide=book();wide['asks'][0][0]='200'
        put(self.o,'book',T+8,wide,timing=stamp(T+8));self.o.tick(T+9,stamp(T+9))
        self.assertEqual(self.o.l.all('decision_retry'),[])
    def test_release_crash_is_atomic_and_retry_is_idempotent(self):
        from unittest.mock import patch
        uncertain(self.o);add=self.o.l.add
        def crash(kind,key,body):
            value=add(kind,key,body)
            if kind=='interpretation_release':raise RuntimeError('CRASH')
            return value
        with patch.object(self.o.l,'add',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'CRASH'):self.o.tick(T+2,stamp(T+2))
        self.assertEqual(self.o.l.all('interpretation_release'),[])
        self.o.tick(T+2,stamp(T+2));self.o.tick(T+2,stamp(T+2))
        self.assertEqual(len(self.o.l.all('interpretation_release')),4)
    def test_epoch_retry_crash_restarts_with_one_generation(self):
        from unittest.mock import patch
        for s,t,d in sequence()[:5]:put(self.o,s,t,d)
        put(self.o,'metadata',T+6,metadata('BREAK'),timing=stamp(T+6));old=self.o.l.last('decision');add=self.o.l.add
        def crash(kind,key,body):
            value=add(kind,key,body)
            if kind=='decision_retry':raise RuntimeError('CRASH')
            return value
        with patch.object(self.o.l,'add',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'CRASH'):self.o.tick(T+8,stamp(T+8,'B'))
        self.assertIsNone(self.o.l.get('execution',old['id']))
        self.o.close();self.o=Observer(self.root)
        self.o.tick(T+8,stamp(T+8,'B'));self.o.tick(T+9,stamp(T+9,'B'))
        put(self.o,'book',T+10,book(),timing=stamp(T+10,'B'))
        self.assertEqual(len(self.o.l.all('decision_retry')),1);self.assertEqual(len(self.o.l.all('fill')),2)
    def test_fake_live_adapter_charges_calibration_and_release_processing_delay(self):
        # Exercises the live branch with in-memory fake bytes/clocks only. No IO.
        from unittest.mock import patch
        from one_sentinel.transport import validate_approval
        c=self.o.l.get('contract','v1');a={k:c[k] for k in ('policy_hash','account_hash','source_hash')}
        a.update(approved_runtime=str(self.o.l.root),run_id='synthetic-test-run',approval_reference='SYNTHETIC_ONLY_NO_IO',public_data=True,start_at=T-1,stop_at=T+100,model_cash_budget=0,live_orders=False)
        validate_approval(self.o,a,T)
        def live(source,at,value,processed,**kw):
            with patch('one_sentinel.engine.time.time',return_value=at),patch('one_sentinel.engine.capture',return_value=stamp(processed)):
                return put(self.o,source,at,value,classification='REAL_FORWARD',timing=stamp(at),**kw)
        live('clock',T+.6,dict(serverTime=int((T+.3)*1000)),T+.61,context=dict(request_timing=stamp(T)))
        r=live('mini',T+1.3,mini(T+1.29),T+1.31)
        with patch('one_sentinel.pending.capture',return_value=stamp(T+2.1)):
            live('clock',T+1.6,dict(serverTime=int((T+1.595)*1000)),T+1.8,context=dict(request_timing=stamp(T+1.59)))
        release=self.o.l.last('interpretation_release');self.assertIsNotNone(release)
        self.assertEqual(release['proof_timing'],stamp(T+1.8));self.assertEqual(release['available_at'],T+2.1)
        self.assertEqual(self.o.l.get('receipt',r['id'])['received_at'],T+1.3)
        self.assertEqual(self.o.l.get('interpretation',r['id'])['items'][0]['state'],'UNCERTAIN')
    def test_independent_replay_and_three_rehashed_semantic_tampers(self):
        import subprocess,sys
        base=Path(__file__).resolve().parents[1];evidence=self.root.parent/'evidence'
        for script,args in [('replay_pending_epoch.py',['--out',str(evidence)]),('tamper_pending_epoch.py',['--evidence',str(evidence),'--out',str(self.root.parent/'tamper')])]:
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(base/'scripts'/script),*args],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
    def test_pending_raw_tamper_is_dropped_before_release(self):
        self.calibrate();r=put(self.o,'mini',T+1.3,mini(T+1.29),timing=stamp(T+1.3))
        (self.o.l.root/'raw'/(r['sha256']+'.bin')).write_bytes(json.dumps(mini(T+1.29,'200')).encode())
        self.o.tick(T+2,stamp(T+2))
        self.assertEqual(self.o.l.last('clock_pending_result')['reason'],'RAW_CORRUPTION')
        self.assertEqual(self.o.l.all('interpretation_release'),[])

if __name__=='__main__':unittest.main()
