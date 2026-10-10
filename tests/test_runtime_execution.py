import copy,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from one_trading.observer import DAY,DataError,FetchError
from one_trading.runtime import InstanceLock,Journal,cycle,fresh_quotes,validate_clock
from test_learning_execution import bars,config

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.c=config()
    def tearDown(self):self.tmp.cleanup()
    def test_clock_skew(self):
        with self.assertRaises(DataError):validate_clock(1000,999999,self.c)
    def test_stale_quote_excluded(self):
        self.assertEqual(fresh_quotes([{'symbol':'A','closeTime':0,'lastPrice':'100'}],999999,self.c),{})
    def test_zero_quote_excluded(self):
        self.assertEqual(fresh_quotes([{'symbol':'A','closeTime':1000,'lastPrice':'0'}],1000,self.c),{})
    def test_fresh_quote(self):
        self.assertEqual(fresh_quotes([{'symbol':'A','closeTime':1000,'lastPrice':'100'}],1000,self.c),{'A':100.})
    def test_journal_append_only(self):
        j=Journal(self.path/'p.db',self.c)
        with j.db:j.event('TEST',{'ok':True},1)
        with self.assertRaises(sqlite3.IntegrityError):j.db.execute('DELETE FROM events')
        j.close()
    def test_config_change_rejected(self):
        j=Journal(self.path/'p.db',self.c);j.close();c=copy.deepcopy(self.c);c['fee_rate']=.002
        with self.assertRaises(ValueError):Journal(self.path/'p.db',c)
    def test_process_lock(self):
        with InstanceLock(self.path/'a.lock'):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.path/'a.lock'):pass
    def test_network_failure_is_not_success(self):
        class F:
            def fetch(self,held):raise FetchError('network failed')
        r=cycle(self.c,self.path,F())
        self.assertEqual(r['status'],'ERROR_NO_EXECUTION');self.assertFalse(r['book_state_changed'])
        self.assertIsNone(r['last_success_ms'])
    def fixture(self):
        now=int(time.time()*1000);endday=now//DAY*DAY-DAY
        bs=bars(200,endday-199*DAY)
        return {'server_ms':now,'histories':{'BTCUSDT':bs,'AAAUSDT':bs},'prices':{'BTCUSDT':102.,'AAAUSDT':102.},'hashes':{},'universe':['BTCUSDT','AAAUSDT']}
    def test_stale_history_blocks(self):
        s=self.fixture();s['histories']['BTCUSDT']=s['histories']['BTCUSDT'][:-1]
        class F:
            def fetch(self,held):return s
        r=cycle(self.c,self.path,F());self.assertIn('STALE_HISTORY',r['error'])
    def test_mock_forward_cycle_not_a_real_network_claim(self):
        s=self.fixture()
        class F:
            def fetch(self,held):return s
        r=cycle(self.c,self.path,F())
        self.assertEqual(r['status'],'FORWARD_PAPER_CYCLE_OK');self.assertEqual(r['new_simulated_fills'],[])
        self.assertEqual(r['pending_plans'],1)
    def test_repeat_same_daily_signal_dedup(self):
        s=self.fixture()
        class F:
            def fetch(self,held):return s
        r=cycle(self.c,self.path,F());r2=cycle(self.c,self.path,F())
        self.assertEqual(r2['pending_plans'],1)
        self.assertEqual(r2['total_simulated_fills'],0)
    def test_failure_preserves_prior_state(self):
        s=self.fixture()
        class F:
            def fetch(self,held):return s
        class Bad:
            def fetch(self,held):raise FetchError('outage')
        cycle(self.c,self.path,F())
        j=Journal(self.path/'paper.sqlite3',self.c);before=j.get('book');j.close()
        cycle(self.c,self.path,Bad())
        j=Journal(self.path/'paper.sqlite3',self.c);after=j.get('book');j.close();self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
