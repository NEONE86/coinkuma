"""Failure and accounting regression cases. All fixtures here are synthetic."""
import copy,json,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from one_trading.autonomy.config import DEFAULT
from one_trading.autonomy.store import Store,utc,instant,validate_config,process_lock,digest
from one_trading.autonomy.market import checked_clock,closed_bars,derivatives_features,book_view,number
from one_trading.autonomy.model import validate_answer,resolved_memory,invoke
from one_trading.autonomy.policies import evaluate,register,POLICIES
from one_trading.autonomy.paper import execute_book,maintain,account
from one_trading.autonomy.outcomes import resolve_values,mature,compare
from one_trading.autonomy.validation import validate_packet

def book():
    return dict(asks=[[100.01,100000]],bids=[[99.99,100000]],mid=100,spread_bps=2,
                received_at=utc(),receipt_id="synthetic-book")
def packet():
    now=utc();hour=instant(now).replace(minute=0,second=0,microsecond=0).isoformat()
    rows={}
    for i,s in enumerate(DEFAULT["universe"]):
        rows[s]=dict(symbol=s,price_status="VALID",numeric_status="VALID",residual_1h=-.02 if i==0 else .01,
            return_1h=-.01,oi_quantity_change=-.02,last_settled_funding_per_hour=.00001,
            taker_buy_fraction=.5,basis=0,book=book(),bar_end_at=hour,
            oi_quantity_unit="base_asset",oi_notional_unit="USDT",oi_interval_hours=1,oi_end_at=hour,
            observed_funding_interval_hours=8,premium_exchange_time=now)
    return dict(hour=hour,cutoff=utc(),universe=DEFAULT["universe"],assets=rows,receipts=[])
def model_answer():
    return {"assets":[dict(symbol=s,classification="NEUTRAL",action="NO_TRADE",p_excess_6h=.4,
                p_excess_24h=.5,p_excess_72h=.6,rationale="Synthetic observation",counter_hypothesis="No edge")
                for s in DEFAULT["universe"]]}
def call():
    return dict(id="synthetic",status="CONNECTED_VALID",answer=model_answer())

class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Store(self.temp.name);self.c=copy.deepcopy(DEFAULT)
    def tearDown(self):self.s.close();self.temp.cleanup()
    def test_01_wrong_pr_ref_and_capital(self):
        validate_config(self.c)
        for change in ({"base_ref":"main"},{"base_sha":"abc"},{"total_capital_krw":19000000},
                       {"live_trading":True},{"engine_capital_krw":{"CASHFLOW":20000000,"CAPITAL_GROWTH":20000000}},
                       {"fee_bps_per_side":True},{"instrument":"PERPETUAL"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):validate_config(dict(self.c,**change))
    def test_02_timezones_future_and_reversed_receipts(self):
        with self.assertRaises(ValueError):instant("2026-01-01T00:00:00")
        p=packet();p["cutoff"]=(instant(utc())+timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(ValueError,"clock"):validate_packet(self.s,self.c,p)
        p=packet();p["assets"]["BTCUSDT"]["book"]["received_at"]=(instant(p["cutoff"])+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,"future_book"):validate_packet(self.s,self.c,p)
        now=utc();receipt={"requested_at":now,"received_at":now,"id":"x"}
        with self.assertRaisesRegex(ValueError,"CLOCK_SKEW"):checked_clock({"serverTime":instant(now).timestamp()*1000+10000},receipt,3)
    def test_03_oi_quantity_not_notional_and_funding_actual_interval(self):
        h="2026-01-01T08:00:00+00:00";end=int(instant(h).timestamp()*1000)
        oi=[{"timestamp":end-3600000,"sumOpenInterest":"10","sumOpenInterestValue":"1000"},
            {"timestamp":end,"sumOpenInterest":"10","sumOpenInterestValue":"900"}]
        f=[{"fundingTime":end-4*3600000,"fundingRate":".001"},{"fundingTime":end,"fundingRate":".002"}]
        prem={"time":end,"nextFundingTime":end+4*3600000,"markPrice":"90","indexPrice":"90","symbol":"BTCUSDT"}
        r=derivatives_features(oi,f,prem,[],h)
        self.assertEqual(r["oi_quantity_change"],0);self.assertAlmostEqual(r["oi_notional_change"],-.1)
        self.assertEqual(r["observed_funding_interval_hours"],4);self.assertEqual(r["last_settled_funding_per_hour"],.0005)
        self.assertFalse(r["oi_notional_drop_is_liquidation_evidence"])
        with self.assertRaises(ValueError):derivatives_features(oi,[f[-1],f[-1]],prem,[],h)
        p=packet();p["assets"]["BTCUSDT"]["oi_quantity_unit"]="USDT"
        with self.assertRaisesRegex(ValueError,"units"):validate_packet(self.s,self.c,p)
    def test_03b_closed_hour_gaps_bool_and_nonfinite(self):
        for x in (True,float("nan"),float("inf"),None):
            with self.assertRaises(ValueError):number(x)
        with self.assertRaises(ValueError):closed_bars([],"2026-01-01T08:00:00+00:00",utc())
    def test_04_three_policies_common_context(self):
        ds=evaluate(self.c,packet(),call())
        self.assertEqual(len(ds),9);self.assertEqual(len({d["context_hash"] for d in ds}),1)
        self.assertEqual({d["policy"] for d in ds},set(POLICIES))
        ds[0]["context_hash"]="wrong"
        with self.assertRaisesRegex(ValueError,"context"):register(self.s,self.c,packet(),ds)
    def test_05_frozen_registration_and_memory_no_future(self):
        p=packet();ds=evaluate(self.c,p,call());r=register(self.s,self.c,p,ds)
        first=self.s.all("policy_decision")
        ds[0]["action"]="NO_TRADE";register(self.s,self.c,p,ds)
        self.assertEqual(first,self.s.all("policy_decision"))
        future=(instant(p["cutoff"])+timedelta(hours=1)).isoformat()
        self.s.append("outcome","future",dict(status="RESOLVED",resolved_at=future))
        self.assertEqual(resolved_memory(self.s,p["cutoff"]),[])
        with self.assertRaises(ValueError):self.s.append("registration",p["hour"],dict(r,context_hash="changed"))
    def test_06_immature_and_missing_not_scored(self):
        p=packet();register(self.s,self.c,p,evaluate(self.c,p,call()))
        with patch("one_trading.autonomy.outcomes.endpoint_price") as fetch:
            mature(self.s);fetch.assert_not_called()
        self.assertEqual(compare(self.s,self.c)["policies"]["CF_BASE"]["resolved_forecasts"],0)
        q=self.s.all("question")[0]
        with self.assertRaisesRegex(ValueError,"IMMATURE"):resolve_values(q,{},utc())
    def test_06b_known_outcome_recalculation(self):
        deadline=(instant(utc())-timedelta(minutes=1)).replace(second=0,microsecond=0).isoformat()
        q=dict(id="q",symbol="BTCUSDT",horizon_hours=6,deadline=deadline,
               universe=["BTCUSDT","ETHUSDT"],origin_prices={"BTCUSDT":100,"ETHUSDT":100},
               assumed_cost_per_side=.01,stress_cost_per_side=.02)
        prices={s:dict(symbol=s,endpoint=deadline,received_at=utc(),price=v) for s,v in [("BTCUSDT",110),("ETHUSDT",105)]}
        o=resolve_values(q,prices,utc())
        self.assertAlmostEqual(o["net_signal_return"],1.1*.99/1.01-1)
        self.assertAlmostEqual(o["excess_return"],1.1*.99/1.01-1-.05);self.assertTrue(o["label"])
        self.assertLess(o["stress_excess_return"],o["excess_return"])
        prices["ETHUSDT"]["symbol"]="SOLUSDT"
        with self.assertRaisesRegex(ValueError,"identity"):resolve_values(q,prices,utc())
    def test_07_model_unconnected_keeps_numeric(self):
        p=packet();c=dict(self.c,model_backend="unavailable")
        m=invoke(self.s,c,"unconnected",dict(packet=p,memory=[]))
        self.assertEqual(m["status"],"MODEL_NOT_CONNECTED")
        ds=evaluate(c,p,m)
        self.assertTrue(any(d["action"]=="LONG" for d in ds if d["policy"]=="CF_NUMERIC"))
        self.assertTrue(all(d["action"]=="NO_TRADE" and d["probabilities"]["6"] is None for d in ds if d["policy"]=="CF_AI_MEMORY"))
    def test_08_model_schema_rejects_wrong_assets_and_bool(self):
        a=model_answer();validate_answer(a,DEFAULT["universe"])
        for mutate in ("bool","asset","extra"):
            b=copy.deepcopy(a)
            if mutate=="bool":b["assets"][0]["p_excess_6h"]=True
            elif mutate=="asset":b["assets"][0]["symbol"]="CAKEUSDT"
            else:b["assets"][0]["secret"]="extra"
            with self.assertRaises(ValueError):validate_answer(b,DEFAULT["universe"])
    def test_09_restart_and_duplicate_registration(self):
        p=packet();ds=evaluate(self.c,p,call());register(self.s,self.c,p,ds)
        n=self.s.verify();self.s.close();self.s=Store(self.temp.name)
        register(self.s,self.c,p,ds);self.assertEqual(n,self.s.verify())
        with process_lock(self.temp.name):
            with self.assertRaises(ValueError):
                with process_lock(self.temp.name):pass
    def add_intent(self,key="entry",**changes):
        now=instant(utc())
        row=dict(id=key,decision_id=key,policy="CF_BASE",symbol="BTCUSDT",
            registered_at=(now-timedelta(minutes=3)).isoformat(),
            earliest_entry=(now-timedelta(minutes=2)).isoformat(),expires_at=(now+timedelta(hours=1)).isoformat())
        row.update(changes);self.s.append("paper_intent",key,row)
    def test_10_capital_spread_stale_and_missing(self):
        b=book();b["spread_bps"]=100
        with self.assertRaisesRegex(ValueError,"SPREAD"):execute_book(b,"BUY",self.c,quote=100)
        b=book();b["received_at"]=(instant(utc())-timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(ValueError,"STALE"):execute_book(b,"BUY",self.c,quote=100)
        self.add_intent()
        with patch("one_trading.autonomy.paper.fresh_book",return_value=book()):
            maintain(self.s,dict(self.c,max_cf_exposure_krw=1))
        self.assertEqual(self.s.all("paper_rejection")[0]["reason"],"CAPITAL_LIMIT")
        p=packet();p["assets"]["BTCUSDT"]["price_status"]="DATA_MISSING"
        self.assertTrue(all(d["action"]=="NO_TRADE" for d in evaluate(self.c,p,call()) if d["symbol"]=="BTCUSDT"))
    def test_11_delayed_duplicate_positions_and_exact_cash(self):
        self.add_intent()
        with patch("one_trading.autonomy.paper.fresh_book",side_effect=lambda *a:book()):
            maintain(self.s,self.c);maintain(self.s,self.c)
            self.add_intent("second");maintain(self.s,self.c)
        self.assertEqual(len(self.s.all("paper_entry")),1)
        self.assertEqual(self.s.get("paper_rejection","second")["reason"],"DUPLICATE_OPEN_SYMBOL")
        entry=self.s.all("paper_entry")[0]
        self.assertAlmostEqual(account(self.s,self.c,"CF_BASE")["cash_krw"],12000000-entry["cost_krw"])
        self.assertEqual(entry["funding_status"],"NOT_APPLICABLE_SPOT")
    def test_11b_no_impossible_depth_or_perpetual_funding(self):
        b=book();b["asks"]=[[100.01,.00001]]
        with self.assertRaisesRegex(ValueError,"DEPTH"):execute_book(b,"BUY",self.c,quote=100)
        with self.assertRaisesRegex(ValueError,"FUNDING"):execute_book(book(),"BUY",dict(self.c,instrument="PERPETUAL"),quote=100)
    def test_11c_exit_accounting_once(self):
        self.add_intent()
        with patch("one_trading.autonomy.paper.fresh_book",side_effect=lambda *a:book()):
            maintain(self.s,dict(self.c,hold_hours=0))
            maintain(self.s,self.c);maintain(self.s,self.c)
        self.assertEqual(len(self.s.all("paper_exit")),1)
        e=self.s.all("paper_entry")[0];x=self.s.all("paper_exit")[0]
        self.assertAlmostEqual(x["realized_pnl_krw"],x["cash_delta_krw"]-e["cost_krw"])
        self.assertLess(x["realized_pnl_krw"],0)
        self.assertAlmostEqual(account(self.s,self.c,"CF_BASE")["cash_krw"],12000000+x["realized_pnl_krw"])
    def test_11d_loss_limit_no_entry(self):
        self.add_intent()
        losing=dict(cash_krw=12000000,open_cost_krw=0,open_positions=[],equity_krw=10000000)
        with patch("one_trading.autonomy.paper.fresh_book",return_value=book()),patch("one_trading.autonomy.paper.account",return_value=losing):
            maintain(self.s,self.c)
        self.assertEqual(self.s.get("paper_rejection","entry")["reason"],"LOSS_LIMIT")


class GrowthAndFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Store(self.temp.name);self.c=copy.deepcopy(DEFAULT)
    def tearDown(self):self.s.close();self.temp.cleanup()
    def test_12_distinct_growth_causes_and_four_controls(self):
        from one_trading.autonomy.growth import register_growth,validate_causes,growth_schema
        causes=[]
        for i in range(3):
            causes.append(dict(id=str(i),mechanism="synthetic-cause-"+str(i),
                root_url="https://solana.com/synthetic-"+str(i),anchor="synthetic",
                direct_symbol="SOLUSDT",indirect_symbols=["JUPUSDT","PYTHUSDT"],
                observed_claim="synthetic",assumptions="unknown",competitors="other",
                harm_candidates="unknown",economic_test="two future observations",invalidation="no adoption"))
        bad=copy.deepcopy(causes);bad[1]["mechanism"]=bad[0]["mechanism"]
        with self.assertRaises(ValueError):validate_causes(bad)
        rows=[]
        for c in causes:
            a={k:"synthetic" for k in growth_schema()["properties"]["forecasts"]["items"]["required"]}
            a.update(cause_id=c["id"],selected_symbol="PYTHUSDT",action="WATCH",p_economic_90d=.4,p_excess_180d=.3)
            rows.append(a)
        model=dict(id="synthetic-growth-model",status="CONNECTED_VALID",answer={"forecasts":rows})
        def source(store,url):
            return dict(id=url,received_at=utc(),sha256="synthetic"),"synthetic source"
        with patch("one_trading.autonomy.growth.capture_source",side_effect=source),patch("one_trading.autonomy.growth.fresh_book",side_effect=lambda *a:book()),patch("one_trading.autonomy.growth.invoke",return_value=model):
            result=register_growth(self.s,self.c,dict(causes=causes,support_urls=[],support_claims=[]))
        self.assertEqual(len(result["controls"]),12)
        self.assertEqual(len({x["context_hash"] for x in result["controls"]}),1)
        self.assertEqual({x["policy"] for x in result["controls"]},{"G_DIRECT","G_SECTOR","G_INDIRECT","G_LAGGED"})
        self.assertEqual(result["cash_krw"],8000000)
        self.assertGreater((instant(result["economic_deadline"])-instant(utc())).total_seconds(),89*86400)
        self.assertEqual(result["paper_positions"],[])
    def test_invalid_growth_boolean_and_direct_selection(self):
        from one_trading.autonomy.growth import validate_growth_answer,growth_schema
        causes=[dict(id=str(i),direct_symbol="SOLUSDT",indirect_symbols=["JUPUSDT","PYTHUSDT"]) for i in range(3)]
        rows=[]
        for c in causes:
            a={k:"synthetic" for k in growth_schema()["properties"]["forecasts"]["items"]["required"]}
            a.update(cause_id=c["id"],selected_symbol="PYTHUSDT",action="WATCH",p_economic_90d=.4,p_excess_180d=.3);rows.append(a)
        validate_growth_answer({"forecasts":rows},causes)
        rows[0]["p_economic_90d"]=True
        with self.assertRaises(ValueError):validate_growth_answer({"forecasts":rows},causes)
        rows[0]["p_economic_90d"]=.4;rows[0]["selected_symbol"]="SOLUSDT"
        with self.assertRaises(ValueError):validate_growth_answer({"forecasts":rows},causes)
    def test_actual_child_process_death_rolls_back_and_releases_lock(self):
        import subprocess,sys
        code="from one_trading.autonomy.store import Store,process_lock;import os;"+             "s=Store("+repr(self.temp.name)+");"+             "lock=process_lock("+repr(self.temp.name)+");lock.__enter__();"+             "s.append('durable','one',{'value':1});s.db.execute('BEGIN IMMEDIATE');"+             "s.append('uncommitted','two',{'value':2});os._exit(137)"
        child=subprocess.run([sys.executable,"-c",code],capture_output=True)
        self.assertNotEqual(child.returncode,0)
        self.assertEqual(self.s.get("durable","one")["value"],1)
        self.assertIsNone(self.s.get("uncommitted","two",True))
        with process_lock(self.temp.name):pass
    def test_future_receipt_is_rejected_even_when_price_valid(self):
        p=packet();t=instant(p["cutoff"]);key="reversed"
        self.s.append("http_receipt",key,dict(requested_at=(t+timedelta(seconds=1)).isoformat(),
              received_at=p["cutoff"],error=None,sha256="unused"))
        p["receipts"]=[key]
        with self.assertRaisesRegex(ValueError,"reversed"):validate_packet(self.s,self.c,p)
    def test_mature_missing_endpoint_remains_pending(self):
        p=packet();register(self.s,self.c,p,evaluate(self.c,p,call()))
        q=self.s.all("question")[0]
        future=(instant(q["deadline"])+timedelta(hours=100)).isoformat()
        with patch("one_trading.autonomy.outcomes.utc",return_value=future),patch("one_trading.autonomy.outcomes.endpoint_price",side_effect=ValueError("MISSING")):
            mature(self.s)
        self.assertEqual(self.s.all("outcome"),[])
        self.assertEqual(len(self.s.all("failure")),9)
        self.assertIsNone(compare(self.s,self.c)["policies"]["CF_BASE"]["brier"])
    def test_disk_shortage_and_network_backoff(self):
        from one_trading.autonomy.store import require_disk
        from one_trading.autonomy.market import PublicClient
        from types import SimpleNamespace
        with patch("one_trading.autonomy.store.shutil.disk_usage",return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(OSError,"DISK_LOW"):require_disk(self.temp.name)
        opener=__import__("unittest.mock",fromlist=["Mock"]).Mock()
        opener.open.side_effect=OSError("synthetic network down")
        with patch("one_trading.autonomy.market.build_opener",return_value=opener),patch("one_trading.autonomy.market.time.sleep") as sleep:
            with self.assertRaises(ValueError):PublicClient(self.s).get("https://data-api.binance.vision","/api/v3/time")
        self.assertEqual(len(self.s.all("http_receipt")),3)
        self.assertEqual(sleep.call_count,2)
        with self.assertRaisesRegex(ValueError,"endpoint_not_allowed"):
            PublicClient(self.s).get("https://api.binance.com","/api/v3/order")
    def test_validated_state_machine_resume_not_recollect_or_reregister(self):
        from one_trading.autonomy.engine import tick
        p=packet()
        with patch("one_trading.autonomy.engine.collect",return_value=p) as collection,patch("one_trading.autonomy.engine.invoke",return_value=call()) as model,patch("one_trading.autonomy.engine.maintain",return_value={}),patch("one_trading.autonomy.engine.maintain_growth"):
            interrupted=tick(self.temp.name,self.c,stop_after="packet")
            self.assertEqual(interrupted["status"],"INTERRUPTED_AT_DURABLE_CHECKPOINT")
            first=tick(self.temp.name,self.c);second=tick(self.temp.name,self.c)
        self.assertEqual(collection.call_count,1);self.assertEqual(model.call_count,1)
        self.assertTrue(first["resumed"]);self.assertEqual(second["execution_type"],"REPLAY_MAINTENANCE")
        self.assertEqual(len(self.s.all("policy_decision")),9);self.assertEqual(len(self.s.all("question")),9)
    def test_nonfinite_or_boolean_outcome_rejected(self):
        q=dict(id="q",symbol="BTCUSDT",horizon_hours=6,deadline=(instant(utc())-timedelta(minutes=1)).isoformat(),
               universe=["BTCUSDT","ETHUSDT"],origin_prices={"BTCUSDT":100,"ETHUSDT":100},
               assumed_cost_per_side=.01,stress_cost_per_side=.02)
        prices={s:dict(symbol=s,endpoint=q["deadline"],received_at=utc(),price=True) for s in q["universe"]}
        with self.assertRaises(ValueError):resolve_values(q,prices,utc())


    def test_pr9_ancestry_guard_rejects_main_and_unrelated_head(self):
        from one_trading.autonomy.validation import validate_repository
        from types import SimpleNamespace
        with patch("subprocess.run",return_value=SimpleNamespace(stdout="main\n",returncode=0)):
            with self.assertRaisesRegex(ValueError,"separate"):validate_repository(self.c)
        results=[SimpleNamespace(stdout="research/new\n",returncode=0),SimpleNamespace(returncode=1)]
        with patch("subprocess.run",side_effect=results):
            with self.assertRaisesRegex(ValueError,"not_derived"):validate_repository(self.c)
    def test_deployment_seal_rejects_silent_source_drift(self):
        from one_trading.autonomy.engine import tick
        self.s.append("deployment","sealed",dict(source_hash="not-current"))
        with patch("one_trading.autonomy.engine.collect") as collect:
            with self.assertRaisesRegex(ValueError,"SOURCE_CHANGED"):tick(self.temp.name,self.c)
        collect.assert_not_called()
    def test_resolved_memory_is_nonvacuous_and_future_invariant(self):
        p=packet();now=instant(utc());old=now-timedelta(hours=100)
        origin=copy.deepcopy(p);origin["cutoff"]=old.isoformat();origin["hour"]=old.replace(minute=0,second=0,microsecond=0).isoformat()
        self.s.append("market_packet",origin["hour"],origin)
        q=dict(id="past",symbol="BTCUSDT",horizon_hours=72,registered_at=(old+timedelta(hours=1)).isoformat(),deadline=(old+timedelta(hours=73)).isoformat())
        self.s.append("question","past",q)
        self.s.append("policy_decision","past",dict(hour=origin["hour"],policy="CF_AI_MEMORY",question_ids=["past"],
             probabilities={"72":.8},action="LONG",reason="synthetic forced-reduction hypothesis"))
        o=dict(question_id="past",symbol="BTCUSDT",horizon_hours=72,status="RESOLVED",excess_return=-.03,label=False,
              resolved_at=(old+timedelta(hours=75)).isoformat(),stored_at=(old+timedelta(hours=76)).isoformat())
        self.s.put("outcome","past",o)
        before=resolved_memory(self.s,p["cutoff"],p)
        self.assertEqual(len(before),1);self.assertFalse(before[0]["label"])
        self.assertIn("historical_features",before[0]);self.assertEqual(before[0]["original_predictions"][0]["probability"],.8)
        self.s.put("outcome","future",dict(o,resolved_at=(now+timedelta(hours=1)).isoformat()))
        self.assertEqual(before,resolved_memory(self.s,p["cutoff"],p))


    def test_429_retry_after_survives_restart(self):
        from urllib.error import HTTPError
        from io import BytesIO
        from unittest.mock import Mock
        from one_trading.autonomy.market import PublicClient
        opener=Mock();opener.open.side_effect=HTTPError("https://data-api.binance.vision/api/v3/time",429,"limited",{"Retry-After":"120"},BytesIO(b'{"error":"limited"}'))
        with patch("one_trading.autonomy.market.build_opener",return_value=opener):
            with self.assertRaisesRegex(ValueError,"BACKOFF"):PublicClient(self.s).get("https://data-api.binance.vision","/api/v3/time")
            self.s.close();self.s=Store(self.temp.name)
            with self.assertRaisesRegex(ValueError,"BACKOFF"):PublicClient(self.s).get("https://data-api.binance.vision","/api/v3/time")
        self.assertEqual(opener.open.call_count,1)
        self.assertEqual(len(self.s.all("source_cooldown")),1)
    def test_invalid_model_subprocess_and_timeout_are_isolated(self):
        from types import SimpleNamespace
        import subprocess
        def invalid(command,**kwargs):
            path=Path(command[command.index("--output-last-message")+1])
            path.write_text('{"assets":[]}',encoding="utf-8")
            return SimpleNamespace(returncode=0,stdout="",stderr="")
        p=packet()
        with patch("one_trading.autonomy.model.shutil.which",return_value="synthetic-codex"),patch("one_trading.autonomy.model.subprocess.run",side_effect=invalid):
            result=invoke(self.s,self.c,"invalid-schema",dict(packet=p,memory=[]))
        self.assertEqual(result["status"],"MODEL_INVALID_OUTPUT")
        with patch("one_trading.autonomy.model.shutil.which",return_value="synthetic-codex"),patch("one_trading.autonomy.model.subprocess.run",side_effect=subprocess.TimeoutExpired(["codex"],1)):
            result=invoke(self.s,self.c,"timeout",dict(packet=p,memory=[]))
        self.assertEqual(result["status"],"MODEL_TIMEOUT")
        decisions=evaluate(self.c,p,result)
        self.assertTrue(any(d["action"]=="LONG" for d in decisions if d["policy"]=="CF_BASE"))
        self.assertTrue(all(d["action"]=="NO_TRADE" for d in decisions if d["policy"]=="CF_AI_MEMORY"))

if __name__=="__main__":unittest.main()
