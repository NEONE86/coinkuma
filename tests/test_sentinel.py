import hashlib,importlib.util,json,shutil,tempfile,unittest
from pathlib import Path
from one_sentinel.engine import Observer
from one_sentinel.store import digest,encode
from one_sentinel.demo import T,SYMBOL,metadata,mini,bar,book,sequence
from one_sentinel.notify import analyze_one,dispatch_synthetic,queue_candidate,enqueue
from one_sentinel.paper import account
from one_sentinel.observe import membership

class SentinelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.o=Observer(self.root/'runtime')
    def tearDown(self):self.o.close();self.tmp.cleanup()
    def ingest(self,s,t,data,now=None):return self.o.ingest(s,json.dumps(data).encode(),t,t if now is None else now)
    def ready(self):
        for s,t,d in sequence()[:4]:self.ingest(s,t,d)
    def opened(self):
        for s,t,d in sequence()[:5]:self.ingest(s,t,d)
    def all(self,k):return self.o.l.all(k)
    def test_normal_path_and_independent_recalculation(self):
        for s,t,d in sequence():self.ingest(s,t,d)
        actions={d['action'] for d in self.all('decision')};self.assertTrue({'ENTER','HOLD','REDUCE','EXIT'}<=actions)
        self.assertEqual([f['action'] for f in self.all('fill')],['ENTER','REDUCE','EXIT'])
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'],'0.000')
        result=self.verify_export();self.assertEqual(result['status'],'PASS');self.assertLess(float(result['realized']),0)
    def verify_export(self):
        out=self.root/'export';self.o.export(out);shutil.copytree(self.o.l.root/'raw',out/'raw',dirs_exist_ok=True)
        spec=importlib.util.spec_from_file_location('independent',Path(__file__).parents[1]/'scripts/verify_sentinel.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        return m.verify(out)
    def test_restart_idempotence(self):
        self.opened();before=self.o.status();self.o.close();self.o=Observer(self.root/'runtime')
        for s,t,d in sequence()[:5]:self.ingest(s,t,d)
        self.assertEqual(before,self.o.status())
        for s,t,d in sequence()[5:]:self.ingest(s,t,d)
        self.assertEqual(len(self.all('fill')),3)
    def test_config_change_blocked(self):
        p=dict(self.o.policy,horizon_seconds=3);path=self.root/'policy.json';path.write_text(json.dumps(p))
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):Observer(self.root/'runtime',path)
    def test_existing_runtime_forbidden(self):
        r=self.root/'existing';r.mkdir();(r/'authority.sqlite3').write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'EXISTING_RUNTIME'):Observer(r)
    def test_first_metadata_is_not_event(self):
        self.ingest('metadata',T,metadata('TRADING'));self.assertEqual(self.all('event'),[])
    def test_quiet_price_structural_event_and_alert(self):
        self.ready();self.assertEqual(len(self.all('event')),1);self.assertEqual(self.all('outbox')[0]['kind'],'BASIC_EVENT')
        self.assertEqual(self.all('decision')[-1]['action'],'ENTER')
    def test_rolling_volume_drop_does_not_mean_no_trades(self):
        self.ready();self.ingest('mini',T+4,mini(T+4,q='90000'))
        p=self.all('input')[-1]['parsed'][SYMBOL];self.assertIsNone(p['short_quote_volume']);self.assertEqual(p['rolling_24h_quote_volume'],'90000')
        self.assertEqual([r for r in self.all('input') if r['source']=='bar'][0]['parsed']['quote_volume'],'20000')
    def test_new_symbol_promoted_beyond_initial_five(self):
        self.ready();self.assertIn(SYMBOL,self.all('membership')[-1]['symbols']);self.assertEqual(len(self.all('membership')[-1]['symbols']),5)
    def test_delisted_symbol_removed_unless_open(self):
        self.ready();self.ingest('metadata',T+5,metadata('BREAK'));self.assertNotIn(SYMBOL,self.all('membership')[-1]['symbols'])
    def test_open_delisted_still_monitored(self):
        self.opened();self.ingest('metadata',T+6,metadata('BREAK'));self.assertIn(SYMBOL,self.all('membership')[-1]['symbols']);self.assertEqual(self.all('decision')[-1]['action'],'EXIT')
    def test_bar_outside_deep_rejected(self):
        self.ingest('metadata',T,metadata('BREAK'));self.ingest('bar',T+1,bar(T+1));self.assertEqual(self.all('quarantine')[-1]['error'],'NOT_IN_DEEP_SUBSCRIPTION')
    def test_bar_revision_quarantine_and_dedup(self):
        self.ready();self.ingest('bar',T+4,bar(T+3));self.assertEqual(len(self.all('dedup')),1)
        b=bar(T+3);b['k']['q']='55555';self.ingest('bar',T+5,b);self.assertEqual(self.all('quarantine')[-1]['error'],'CONFLICTING_BAR_REVISION')
    def test_future_and_unclosed_bar(self):
        self.ready();b=bar(T+63);b['k']['x']=False;self.ingest('bar',T+4,b);self.assertEqual(len(self.all('quarantine')),1)
    def test_future_receipt_rejected(self):
        with self.assertRaisesRegex(ValueError,'FUTURE'):self.ingest('mini',T+1,mini(T+1),T)
    def test_future_market_time_quarantined(self):
        self.ingest('mini',T,mini(T+1));self.assertEqual(len(self.all('quarantine')),1)
    def test_unsupported_oi_not_invented(self):
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED'):self.ingest('oi',T,{})
    def test_metadata_boolean_strict(self):
        m=metadata();m['symbols'][0]['isSpotTradingAllowed']='false';self.ingest('metadata',T,m);self.assertEqual(len(self.all('quarantine')),1)
    def test_duplicate_event_suppressed(self):
        self.ready();self.ingest('metadata',T+4,metadata('TRADING'));self.assertEqual(len(self.all('event')),1)
    def test_stale_price_blocks_entry_distinct_unreviewed(self):
        for s,t,d in sequence()[:3]:self.ingest(s,t,d)
        self.ingest('bar',T+30,bar(T+30));self.assertEqual(self.all('decision')[-1]['review_status'],'UNREVIEWED_DATA');self.assertEqual(self.all('fill'),[])
    def test_gap_retains_open_inventory_and_risk(self):
        self.opened();qty=self.o.status()['positions'][SYMBOL]['qty'];self.o.tick(T+700)
        self.assertEqual(self.o.status()['positions'][SYMBOL]['qty'],qty);self.assertEqual(self.all('decision')[-1]['reason'],'DATA_GAP_RETAIN_INVENTORY')
        self.assertIn('RISK',[r['kind'] for r in self.all('outbox')])
    def test_execution_delay_no_same_instant_fill(self):
        self.ready();self.ingest('book',T+3,book());self.assertEqual(self.all('fill'),[]);self.ingest('book',T+5,book());self.assertEqual(len(self.all('fill')),1)
    def test_spread_rejection_releases_reservation(self):
        self.ready();b=book();b['asks'][0][0]='105';self.ingest('book',T+5,b);self.assertEqual(self.all('execution')[0]['reason'],'SPREAD_COST_BLOCKED');self.assertEqual(self.all('fill'),[])
        # A subsequent new decision can reserve; rejected decision's own reservation is excluded.
        self.assertLessEqual(float(self.o.status()['reserved']),1001)
    def test_depth_limit(self):
        self.ready();self.ingest('book',T+5,book(depth='1'));self.assertEqual(self.all('execution')[0]['reason'],'LIQUIDITY_LIMIT')
    def test_expired_decision_rejected(self):
        self.ready();self.o.tick(T+34);self.assertIn('DECISION_EXPIRED',[e['reason'] for e in self.all('execution')])
    def test_add_only_once(self):
        self.opened();self.ingest('mini',T+6,mini(T+6,'103'));self.ingest('book',T+8,book('103'));self.ingest('mini',T+9,mini(T+9,'104'));self.assertEqual([f['action'] for f in self.all('fill')],['ENTER','ADD']);self.assertEqual(self.all('decision')[-1]['action'],'HOLD')
    def test_stop_risk_preempts_pending_add(self):
        self.opened();self.ingest('mini',T+6,mini(T+6,'103'));self.ingest('mini',T+7,mini(T+7,'94'))
        self.assertIn('RISK_PRIORITY_CANCELLED',[r['reason'] for r in self.all('execution')]);self.assertEqual(self.all('decision')[-1]['action'],'EXIT')
    def test_horizon_exit_needs_fresh_execution_book(self):
        self.opened();self.o.tick(T+5+self.o.policy['horizon_seconds']);self.assertEqual(self.all('decision')[-1]['action'],'EXIT');self.assertEqual(len(self.all('fill')),1)
    def test_closed_thesis_not_reentered(self):
        self.opened();self.ingest('mini',T+6,mini(T+6,'94'));self.ingest('book',T+8,book('94'));self.ingest('mini',T+9,mini(T+9,'100'))
        self.assertEqual(self.all('decision')[-1]['reason'],'REVIEWED_THESIS_EXHAUSTED')
    def test_cash_reservation_prevents_second_concurrent_enter(self):
        self.ready();self.assertEqual(self.o.status()['reserved'],'1001.000');self.ingest('mini',T+4,mini(T+4));self.assertEqual(self.o.status()['reserved'],'1001.000')
    def test_basic_notification_disabled_model_and_latency(self):
        self.ready();before=self.all('outbox')[0];r=analyze_one(self.o.l,self.o.policy,T+3);self.assertEqual(r['status'],'UNREVIEWED_MODEL_DISABLED')
        dispatch_synthetic(self.o.l,T+4);self.assertLessEqual(self.all('delivery')[0]['latency_seconds'],60);self.assertFalse(self.all('delivery')[0]['actual_delivery']);self.assertEqual(before,self.all('outbox')[0])
    def test_timeout_does_not_block_basic(self):
        self.ready();r=analyze_one(self.o.l,self.o.policy,T+3,'SYNTHETIC_TIMEOUT');self.assertEqual(r['status'],'UNREVIEWED_TIMEOUT');dispatch_synthetic(self.o.l,T+4);self.assertEqual(len(self.all('delivery')),1)
    def test_queue_full_retains_basic(self):
        self.ready();p=dict(self.o.policy,model_queue_capacity=1)
        with self.o.l.transaction():
            e=self.all('event')[0];q=queue_candidate(self.o.l,p,e,'NEW',T+3);enqueue(self.o.l,e,'RISK',T+3)
        self.assertEqual(q['status'],'UNREVIEWED_QUEUE_FULL');self.assertEqual(len(self.all('outbox')),2)
    def test_six_attempt_cap_and_late_revision(self):
        self.ready();initial=self.all('outbox')[0]
        for category in ['HOLDINGS','NEW','STRUCTURAL']:
            for i in range(2):
                with self.o.l.transaction():
                    key=digest([category,i]);ev=dict(id=key,symbol=SYMBOL,reason='SYNTHETIC',at=T)
                    self.o.l.add('event',key,ev);enqueue(self.o.l,ev,'BASIC_EVENT',T);queue_candidate(self.o.l,self.o.policy,ev,category,T)
        for i in range(10):analyze_one(self.o.l,self.o.policy,T+10+i,'SYNTHETIC_ANSWER',{'action':'NO_ACTION','synthetic':True})
        self.assertEqual(len(self.all('analysis_attempt')),6);self.assertTrue(self.all('analysis_block'));self.assertEqual(initial,self.all('outbox')[0])
        self.assertTrue(any(r['kind']=='AI_ANALYSIS' and r['revision']==1 for r in self.all('outbox')))
    def test_candidate_expiry_not_no_action(self):
        self.ready();analyze_one(self.o.l,self.o.policy,T+1803);self.assertEqual(self.all('analysis_result')[0]['status'],'UNREVIEWED_EXPIRED')
    def test_evidence_not_deleted_after_thirty_days(self):
        self.opened();self.o.tick(T+40*86400);self.assertTrue(all(not p['automatic_delete'] for p in self.all('evidence_pin')));self.assertTrue(list((self.o.l.root/'raw').glob('*.bin')))
    def test_independent_verifier_rejects_rehashed_wrong_fee(self):
        self.opened();self.verify_export();p=self.root/'export/ledger.jsonl';rows=[json.loads(x) for x in p.read_text().splitlines()];prev='0'*64
        for r in rows:
            b=json.loads(r['body'])
            if r['kind']=='fill':b['fee']='0'
            r['body']=encode(b);r['prev']=prev;r['hash']=digest([prev,r['kind'],r['id'],b]);prev=r['hash']
        p.write_text('\n'.join(encode(r) for r in rows)+'\n')
        spec=importlib.util.spec_from_file_location('v',Path(__file__).parents[1]/'scripts/verify_sentinel.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with self.assertRaisesRegex(AssertionError,'FILL_COST'):m.verify(p.parent)
    def test_raw_corruption_rejected(self):
        self.ready();p=next((self.o.l.root/'raw').glob('*.bin'));p.write_bytes(b'corrupt')
        with self.assertRaisesRegex(AssertionError,'RAW_HASH'):self.verify_export()
    def test_delayed_processing_latency_measured_from_receipt(self):
        self.ingest('metadata',T,metadata());self.ingest('metadata',T+1,metadata('TRADING'),T+100);dispatch_synthetic(self.o.l,T+101)
        self.assertEqual(self.all('delivery')[0]['latency_seconds'],100)
    def test_global_processing_clock_regression_quarantined(self):
        self.ready();self.ingest('book',T+1,book());self.assertEqual(self.all('quarantine')[-1]['error'],'PROCESSING_CLOCK_REGRESSION');self.assertEqual(self.all('fill'),[])
    def test_missing_instrument_filters_blocks_entry(self):
        self.ingest('metadata',T,metadata());self.ingest('mini',T+1,mini(T+1));m=metadata('TRADING')
        for s in m['symbols']:s.pop('filters')
        self.ingest('metadata',T+2,m);self.ingest('bar',T+3,bar(T+3));self.assertEqual(self.all('decision')[-1]['reason'],'UNSUPPORTED_INSTRUMENT_FILTERS')
    def test_marked_concentration_reduces_position(self):
        self.opened();self.ingest('mini',T+6,mini(T+6,'210'));self.assertEqual(self.all('decision')[-1]['reason'],'MARKED_EXPOSURE_LIMIT')
    def test_metadata_scope_change_not_delisting(self):
        self.ready();raw=json.dumps(metadata('BREAK')).encode();self.o.ingest('metadata',raw,T+4,T+4,origin='https://example.invalid/partial')
        self.assertEqual(self.all('quarantine')[-1]['error'],'METADATA_SCOPE_CHANGED');self.assertEqual(len(self.all('event')),1)
    def test_candidate_release_after_ttl(self):
        self.ready()
        with self.o.l.transaction():chosen=membership(self.o.l,self.o.policy,T+22000)
        self.assertNotIn(SYMBOL,chosen)
    def test_future_candidate_not_reviewed_by_backdated_call(self):
        self.ready();before=len(self.all('analysis_block'));r=analyze_one(self.o.l,self.o.policy,T)
        self.assertEqual(r['status'],'NO_ELIGIBLE_ANALYSIS');self.assertEqual(len(self.all('analysis_block')),before)
    def test_duplicate_receipt_has_no_new_ledger_rows(self):
        self.ready();before=self.o.l.db.execute('select count(*) from records').fetchone()[0]
        r=self.ingest('metadata',T+2,metadata('TRADING'));self.assertTrue(r['duplicate']);self.assertEqual(self.o.l.db.execute('select count(*) from records').fetchone()[0],before)

if __name__=='__main__':unittest.main()
