import copy
import math
import unittest
from test_ai_native import packet,BASE,SequencePlanner
from datetime import timedelta
from dataclasses import asdict
from one_trading.ai_native.engine import Protocol,Experiment,ModelDecision
from one_trading.ai_native.causal_features import build
from one_trading.ai_native.planners import plan,CashPlanner
from one_trading.ai_native.audit import verify

class Features(unittest.TestCase):
    def frames(self):return [packet(i,100+i*.01,second_step=1) for i in range(361)]
    def test_manual_returns_volatility_and_prefix_invariance(self):
        f=self.frames();cut=f[300]["received_at"];p=asdict(Protocol())
        a=build(f,cut,p);b=build(f[:301],cut,p);self.assertEqual(a,b)
        w=a["symbols"]["BTCUSDT"]["windows"]["30"]
        self.assertAlmostEqual(w["return_fraction"],103.05/102.75-1)
        expected=math.sqrt(sum(math.log((100.05+i*.01)/(100.05+(i-1)*.01))**2 for i in range(271,301)))
        self.assertAlmostEqual(w["realized_vol"],expected)
        self.assertIsNone(a["symbols"]["BTCUSDT"]["expected_net_return"])
        self.assertEqual(a,build(f,cut,p))
    def test_gaps_short_stale_and_duplicate(self):
        f=self.frames();p=asdict(Protocol())
        self.assertEqual(build(f[:10],f[9]["received_at"],p)["symbols"]["BTCUSDT"]["windows"]["30"]["status"],"INSUFFICIENT_HISTORY")
        g=f[:285]+f[290:301]
        self.assertEqual(build(g,f[300]["received_at"],p)["symbols"]["BTCUSDT"]["windows"]["30"]["status"],"GAP")
        with self.assertRaises(ValueError):build(f[:2]+f[1:3],f[2]["received_at"],p)
        with self.assertRaisesRegex(ValueError,"STALE"):build(f[:1],f[10]["received_at"],p)
    def test_depth_imbalance_cost_and_no_false_positive_reward(self):
        f=self.frames();f[-1]["quotes"]["BTCUSDT"].update(bid_size=3,ask_size=1)
        a=build(f,f[-1]["received_at"],asdict(Protocol()))["symbols"]["BTCUSDT"]
        self.assertEqual(a["imbalance"],.5);self.assertGreater(a["round_trip_cost_bps"],30)
        self.assertFalse(a["long_structure_candidate"]["meets_2_to_1"])

class RiskPaths(unittest.TestCase):
    def replay(self,frames,protocol=None,actions=None,source_times=None):
        class Inject:
            def propose(self,c):
                action=(actions or {"source-0":"BUY","source-3":"SELL"}).get(c["frame_id"],"HOLD")
                return ModelDecision(plan(action,"BTCUSDT",1. if action!="HOLD" else 0.,"SYNTHETIC INJECTION"),.5)
        e=Experiment(protocol or Protocol(),{"TEST":Inject(),"CASH":CashPlanner()},data_use="SYNTHETIC")
        for f in frames:e.observe(f,source_received_at=source_times)
        result=e.finish();verify(result);return result
    def test_delayed_buy_sell_fees_and_position_cap(self):
        r=self.replay([packet(i,100+i,second_step=3) for i in range(6)])
        fills=[x["payload"] for x in r["ledger"] if x["kind"]=="paper_fill"]
        self.assertEqual(len(fills),2);self.assertEqual(r["summary"]["TEST"]["open_positions"],0)
        self.assertAlmostEqual(fills[0]["quantity"]*fills[0]["execution_price"],1250)
        self.assertGreater(r["summary"]["TEST"]["fees_paid"],0)
        self.assertGreater(r["summary"]["TEST"]["modeled_slippage_paid"],0)
    def test_predecision_stale_spread_and_depth(self):
        for kind,reason in [("old","PRE_DECISION_SOURCE_QUOTE"),("stale","STALE_QUOTE"),("spread","SPREAD_LIMIT"),("depth","INSUFFICIENT_VISIBLE_ASK")]:
            with self.subTest(kind=kind):
                f=[packet(0,second_step=3),packet(1,second_step=3,age=40 if kind=="stale" else 0,gap=5 if kind=="spread" else .1,ask_depth=.001 if kind=="depth" else 50000)]
                r=self.replay(f,source_times={"BTCUSDT":BASE.isoformat()} if kind=="old" else None)
                self.assertIn(reason,[x["payload"]["reason"] for x in r["ledger"] if x["kind"]=="order_rejected"])
    def test_drawdown_halt_and_exposure(self):
        # Existing suite covers multiple assets; explicitly test cap with a smaller total cap.
        p=Protocol(max_position_fraction=.1,max_total_exposure_fraction=.11)
        f=[packet(i,100,second_step=3) for i in range(5)]
        for x in f:x["quotes"]["ETHUSDT"]=copy.deepcopy(x["quotes"]["BTCUSDT"])
        class Two:
            def propose(self,c):
                s="BTCUSDT" if c["frame_id"]=="source-0" else "ETHUSDT"
                return ModelDecision(plan("BUY",s,.1,"synthetic"),.5)
        e=Experiment(p,{"TEST":Two(),"CASH":CashPlanner()},data_use="SYNTHETIC")
        for x in f:e.observe(x)
        r=e.finish();verify(r)
        self.assertIn("TOTAL_EXPOSURE_LIMIT",[x["payload"]["reason"] for x in r["ledger"] if x["kind"]=="order_rejected"])
        # Large existing exposure under an explicitly synthetic protocol triggers the latch.
        p=Protocol(max_position_fraction=.8,max_total_exposure_fraction=.9)
        r=self.replay([packet(i,100 if i<2 else 50,second_step=3) for i in range(5)],protocol=p,actions={"source-0":"BUY","source-2":"BUY"})
        self.assertTrue(r["summary"]["TEST"]["drawdown_halted"])

    def test_sell_insufficient_visible_bid(self):
        f=[packet(i,100,second_step=3,bid_depth=.001 if i==4 else 50000) for i in range(6)]
        r=self.replay(f)
        self.assertIn("INSUFFICIENT_VISIBLE_BID",[x["payload"]["reason"] for x in r["ledger"] if x["kind"]=="order_rejected"])
        self.assertEqual(r["summary"]["TEST"]["fills"],1)
        self.assertEqual(r["summary"]["TEST"]["open_positions"],1)
