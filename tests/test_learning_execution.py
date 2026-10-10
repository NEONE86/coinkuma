import copy,json,math,tempfile,unittest
from pathlib import Path
from one_trading.learning import D,HistoryPanel,MaturedRidge,feature,solve
from one_trading.observer import DAY,Bar
from one_trading.portfolio import Book,Position,make_plan,validate_config
ROOT=Path(__file__).resolve().parents[1]

def config():return json.loads((ROOT/'config.json').read_text())

def plan(id='a',due=DAY,weight=.2,risk=.005):
    return {'id':id,'execute_after_ms':due,'expires_ms':due+DAY-1,'position_context':{},
            'targets':[{'symbol':'AAAUSDT','target_weight':weight,'risk_fraction':risk,
                        'reference_price':100.,'stop':90.,'allow_add':True,'stage':'PROBE'}]}

def bars(n=240,start=1704067200000):
    return [Bar(start+i*DAY,start+(i+1)*DAY-1,100+i*.01,102+i*.01,98+i*.01,100+i*.01,1000,100000) for i in range(n)]

class LearningTests(unittest.TestCase):
    def test_linear_solve(self):self.assertEqual(solve([[2.,0.],[0.,4.]],[4.,8.]),[2.,2.])
    def test_singular_rejected(self):
        with self.assertRaises(ValueError):solve([[0.]], [1.])
    def test_warmup_returns_none(self):self.assertIsNone(MaturedRidge(config()).predict([1.]*D))
    def test_future_labels_blocked(self):
        with self.assertRaises(ValueError):MaturedRidge(config()).learn(2,[{'x':[1.]*D,'ys':[0.,0.]}],1)
    def test_duplicate_training_dates(self):
        m=MaturedRidge(config());r=[{'x':[1.]*D,'ys':[0.,0.]}];m.learn(1,r,1)
        with self.assertRaises(ValueError):m.learn(1,r,2)
    def test_nonfinite_sample(self):
        with self.assertRaises(ValueError):MaturedRidge(config()).learn(1,[{'x':[float('nan')]*D,'ys':[0.,0.]}],1)
    def test_date_weight_not_coin_count(self):
        c=config();c.update(min_training_dates=1,min_training_samples=1)
        a,b=MaturedRidge(c),MaturedRidge(c);r={'x':[1.]+[0.]*(D-1),'ys':[.1,.2]}
        a.learn(1,[r],1);b.learn(1,[r]*20,1);a.fit(1);b.fit(1)
        for x,y in zip(a.predict(r['x']),b.predict(r['x'])):self.assertAlmostEqual(x,y)
    def test_refit_cadence(self):
        c=config();c.update(min_training_dates=1,min_training_samples=1)
        m=MaturedRidge(c);m.learn(1,[{'x':[1.]*D,'ys':[.1,.1]}],1)
        self.assertTrue(m.fit(1));self.assertFalse(m.fit(2));self.assertTrue(m.fit(8*DAY))
    def test_future_changes_do_not_change_features(self):
        a=bars(260);h={'BTCUSDT':a,'AAAUSDT':copy.deepcopy(a)};p=HistoryPanel(h,config());day=a[220].open_ms
        first=p.features_on(day)
        h['AAAUSDT'][-1]=Bar(a[-1].open_ms,a[-1].close_ms,999,1000,998,999,1,1)
        second=HistoryPanel(h,config()).features_on(day)
        self.assertEqual(first,second)
    def test_label_uses_delayed_entry(self):
        a=bars(260);p=HistoryPanel({'BTCUSDT':a,'AAAUSDT':a},config())
        group=p.matured_group(a[-1].open_ms)
        self.assertEqual(len(group),1)
        c=config();cost=math.log((1+c['slippage_rate'])*(1+c['fee_rate'])/((1-c['slippage_rate'])*(1-c['fee_rate'])))
        self.assertAlmostEqual(group[0]['ys'][0],math.log(a[-1].close/a[-28].open)-cost)

class ExecutionTests(unittest.TestCase):
    def setUp(self):self.c=config();self.book=Book(10000);self.prices={'AAAUSDT':100.}
    def buy(self):self.book.begin_day(DAY,self.prices);self.book.apply_plan(plan(),self.prices,DAY,self.c)
    def test_no_live_mode(self):
        self.c['mode']='LIVE'
        with self.assertRaises(ValueError):validate_config(self.c)
    def test_nan_config(self):
        self.c['profit_take_R']=float('nan')
        with self.assertRaises(ValueError):validate_config(self.c)
    def test_no_early_fill(self):
        with self.assertRaises(ValueError):self.book.apply_plan(plan(),self.prices,DAY-1,self.c)
        self.assertEqual(self.book.cash,10000)
    def test_expired_plan(self):
        self.assertIn('EXPIRED',self.book.apply_plan(plan(),self.prices,3*DAY,self.c)[0]);self.assertFalse(self.book.positions)
    def test_restart_idempotency(self):
        self.buy();restored=Book.from_dict(self.book.to_dict());before=restored.cash
        restored.apply_plan(plan(),self.prices,DAY,self.c);self.assertEqual(before,restored.cash)
    def test_old_id_cannot_replay_after_another_plan(self):
        self.buy();self.book.apply_plan(plan('b'),self.prices,DAY,self.c);n=len(self.book.fills)
        self.book.apply_plan(plan(),self.prices,DAY,self.c);self.assertEqual(n,len(self.book.fills))
    def test_buy_cash_and_cost(self):
        self.buy();p=self.book.positions['AAAUSDT'];self.assertAlmostEqual(10000-self.book.cash,p.cost_basis)
        self.assertGreater(p.average_entry,100);self.assertGreater(self.book.total_fees,0)
    def test_roundtrip_pnl_reconciles(self):
        self.buy();self.book.liquidate({'AAAUSDT':110},2*DAY,self.c,'TEST')
        self.assertAlmostEqual(self.book.cash-10000,self.book.realized_pnl)
    def test_risk_cap(self):
        self.buy();self.assertLessEqual(self.book.open_risk(self.prices,self.c),50+1e-7)
    def test_plan_cannot_raise_individual_risk(self):
        self.book.apply_plan(plan(risk=.8),self.prices,DAY,self.c)
        self.assertLessEqual(self.book.open_risk(self.prices,self.c),50+1e-7)
    def test_no_chase_entry_gap(self):
        self.book.apply_plan(plan(),{'AAAUSDT':105.},DAY,self.c);self.assertFalse(self.book.positions)
    def test_invalidated_entry(self):
        self.book.apply_plan(plan(),{'AAAUSDT':85.},DAY,self.c);self.assertFalse(self.book.positions)
    def test_missing_held_quote_blocks(self):
        self.buy()
        with self.assertRaises(ValueError):self.book.monitor_quotes({},2*DAY,self.c)
    def test_stop_gap_worse_open(self):
        self.buy();b=Bar(2*DAY,3*DAY-1,80,85,75,81,1,1)
        self.book.monitor_bar({'AAAUSDT':b},b.close_ms,self.c)
        self.assertEqual(self.book.fills[-1]['reference_price'],80.)
    def test_stop_first_if_both_hit(self):
        self.buy();b=Bar(2*DAY,3*DAY-1,100,150,85,120,1,1)
        self.book.monitor_bar({'AAAUSDT':b},b.close_ms,self.c)
        self.assertEqual(self.book.fills[-1]['reason'],'BAR_STOP_FIRST')
    def test_partial_tp_once(self):
        self.buy();self.book.monitor_quotes({'AAAUSDT':140},2*DAY,self.c)
        n=len(self.book.fills);self.book.monitor_quotes({'AAAUSDT':140},2*DAY+1,self.c)
        self.assertEqual(n,len(self.book.fills));self.assertTrue(self.book.positions['AAAUSDT'].took_profit)
    def test_partial_cost_basis_proportional(self):
        self.buy();p=self.book.positions['AAAUSDT'];cost=p.cost_basis;qty=p.qty
        self.book.sell('AAAUSDT',qty/2,110,2*DAY,self.c,'PARTIAL')
        self.assertAlmostEqual(self.book.positions['AAAUSDT'].cost_basis,cost/2)
    def test_drawdown_latched(self):
        self.book.peak=12000;self.book.risk_trigger(DAY,{},self.c)
        self.assertTrue(self.book.halted)
        self.book.cash=13000;self.book.risk_trigger(DAY,{},self.c);self.assertTrue(self.book.halted)
    def test_daily_loss_blocks_new_orders(self):
        self.book.day_start_equity=11000;self.book.current_day=1
        self.book.apply_plan(plan(),self.prices,DAY,self.c);self.assertFalse(self.book.positions)
    def test_cash_never_negative(self):
        self.book.cash=25;self.book.apply_plan(plan(),self.prices,DAY,self.c);self.assertGreaterEqual(self.book.cash,0)
    def test_stop_never_widens(self):
        self.buy();p=plan('b');p['targets'][0]['stop']=80
        p['position_context']={'AAAUSDT':DAY}
        self.book.apply_plan(p,self.prices,DAY,self.c);self.assertGreaterEqual(self.book.positions['AAAUSDT'].stop,90.)
    def test_plan_is_due_after_observation_day(self):
        a=bars();r=feature('AAAUSDT',a,a);m=MaturedRidge(self.c)
        observed=a[-1].close_ms+8*3600000
        p=make_plan([r],m,self.book,self.prices,observed,self.c)
        self.assertGreater(p['execute_after_ms'],observed)

if __name__=='__main__':unittest.main()
