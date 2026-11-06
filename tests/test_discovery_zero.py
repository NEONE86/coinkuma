"""Synthetic-only software tests; do NOT infer trading edge."""
from __future__ import annotations

import csv
import json
import math
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from one_trading.discovery_zero.__main__ import baseline_hold, create_demo, run, verify
from one_trading.discovery_zero.audit import reconcile
from one_trading.discovery_zero.evolve import SearchConfig, search, splits
from one_trading.discovery_zero.genome import Genome, canonical, mutate
from one_trading.discovery_zero.market import DataError, load_csv
from one_trading.discovery_zero.replay import Limits, simulate


class DiscoveryZeroTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir=Path(self.temp.name)
        self.path=self.dir/'synthetic.csv'
        create_demo(self.path,rows=460)
        self.m=load_csv(self.path,source_type='SYNTHETIC_DEMO')
        self.lim=Limits(starting_cash=10000,fee_bps=8,slippage_bps=7)
        self.cash=Genome.from_dict({'type':'CASH'},self.m.active_features)

    def test_synthetic_provenance_and_panel(self):
        self.assertFalse(self.m.source_type!='SYNTHETIC_DEMO')
        self.assertTrue(self.m.funding_known)
        self.assertEqual(len(self.m.times),460)
        self.assertEqual(len(self.m.symbols),3)
        self.assertNotIn('oi_change',self.m.active_features)

    def test_invalid_executable_in_genome_rejected(self):
        template={'type':'EXPRESSION','scores':{'LONG':"__import__('os').system('false')",'SHORT':0,'FLAT':0,'HOLD':0},'size':1}
        with self.assertRaisesRegex(ValueError,'UNKNOWN_FEATURE'):
            Genome.from_dict(template,self.m.active_features)

    def test_invalid_depth_and_operator(self):
        p={'type':'EXPRESSION','scores':{'LONG':['exec','r1'],'SHORT':0,'FLAT':0,'HOLD':0},'size':1}
        with self.assertRaisesRegex(ValueError,'BAD_OPERATOR'):
            Genome.from_dict(p,self.m.active_features)

    def test_no_cross_asset_timestamp_holes(self):
        with self.path.open(encoding='utf-8') as f:rows=list(csv.DictReader(f))
        rows.pop(25)
        p=self.dir/'hole.csv'
        with p.open('w',newline='',encoding='utf-8')as f:
            writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
        with self.assertRaisesRegex(DataError,'SYMBOL_TIMESTAMP_MISMATCH'):
            load_csv(p)

    def test_duplicate_timestamp_detected(self):
        with self.path.open(encoding='utf-8') as f: rows=list(csv.DictReader(f))
        row=dict(rows[-3]);row['timestamp']=rows[-6]['timestamp']
        rows[-3]=row
        p=self.dir/'duplicated.csv'
        with p.open('w',newline='',encoding='utf-8')as f:
            writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
        with self.assertRaises(DataError):load_csv(p)

    def test_cash_no_trades_no_cost(self):
        r=simulate(self.m,self.cash,20,450,self.lim,capture=True)
        self.assertEqual(r.net_multiple,1.)
        self.assertEqual(r.costs,0.)
        self.assertEqual(r.fills,0)
        self.assertEqual(len(r.equity_points),430)

    def test_next_open_execution_not_same_bar(self):
        b=baseline_hold(self.m.active_features)
        out=simulate(self.m,b,20,80,self.lim,capture=True)
        fills=[e for e in out.events if e['event']=='FILL']
        self.assertTrue(fills)
        self.assertEqual(fills[0]['t'],self.m.times[21].isoformat())
        signals=[e for e in out.events if e['event']=='DECISION']
        self.assertEqual(signals[0]['t'],self.m.times[20].isoformat())
        self.assertTrue(all(e['price']==self.m.series[e['s']][21].o for e in fills if e['t']==fills[0]['t']))

    def test_action_hold_does_not_trade_every_bar(self):
        out=simulate(self.m,baseline_hold(self.m.active_features),20,150,self.lim,capture=True)
        self.assertLess(out.fills,20)
        self.assertGreater(out.fills,0)

    def test_economic_audit_on_nonzero_trade(self):
        r=simulate(self.m,baseline_hold(self.m.active_features),20,150,self.lim,capture=True)
        report=reconcile(self.m,(20,150),self.lim,r.events,r.equity_points,r.summary())
        self.assertEqual(report['status'],'PASS_ACCOUNTING_RECONCILIATION_ONLY')
        self.assertGreater(report['checked_fills'],0)

    def test_forged_fill_cost_rejected_even_with_matching_summary(self):
        r=simulate(self.m,baseline_hold(self.m.active_features),20,150,self.lim,capture=True)
        events=json.loads(json.dumps(r.events))
        for x in events:
            if x['event']=='FILL':x['cost']*=.5;break
        with self.assertRaisesRegex(ValueError,'AUDIT_COST_INCONSISTENT'):
            reconcile(self.m,(20,150),self.lim,events,r.equity_points,r.summary())

    def test_equity_tampering_rejected(self):
        r=simulate(self.m,baseline_hold(self.m.active_features),20,150,self.lim,capture=True)
        equity=json.loads(json.dumps(r.equity_points))
        equity[3][1]+=100
        with self.assertRaisesRegex(ValueError,'AUDIT_EQUITY_RECONCILIATION'):
            reconcile(self.m,(20,150),self.lim,r.events,equity,r.summary())

    def test_future_prefix_invariance(self):
        g=baseline_hold(self.m.active_features)
        base=simulate(self.m,g,20,220,self.lim,capture=True)
        with self.path.open(encoding='utf-8') as f:rows=list(csv.DictReader(f))
        for j,row in enumerate(rows):
            if j//3>230:
                row['open']=str(float(row['open'])*1.5)
                row['high']=str(float(row['high'])*1.5)
                row['low']=str(float(row['low'])*1.5)
                row['close']=str(float(row['close'])*1.5)
                row['volume']=str(float(row['volume'])*2)
        altered=self.dir/'altered.csv'
        with altered.open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
        other=load_csv(altered,source_type='SYNTHETIC_DEMO')
        a=simulate(other,g,20,220,self.lim,capture=True)
        self.assertEqual(a.events,base.events)
        self.assertEqual(a.equity_points,base.equity_points)

    def test_short_pnl_on_falling_tape(self):
        p=self.dir/'down.csv';t=self.m.times
        with p.open('w',newline='',encoding='utf-8')as f:
            w=csv.writer(f);w.writerow(['timestamp','symbol','open','high','low','close','volume'])
            price=1000.
            for i in range(460):
                op=price;price=op*.996
                w.writerow([t[i].isoformat(),'TESTUSDT',op,max(op,price)*1.001,min(op,price)*.999,price,100000])
        market=load_csv(p)
        g=Genome.from_dict({'type':'EXPRESSION','scores':{'LONG':-2,'SHORT':2,'FLAT':-2,'HOLD':['mul',3,['abs','position']]},'size':3},market.active_features)
        r=simulate(market,g,20,400,Limits(fee_bps=0,slippage_bps=0))
        self.assertGreater(r.net_multiple,1.)

    def test_mutation_produces_valid_programs(self):
        import random
        rng=random.Random(39)
        g=baseline_hold(self.m.active_features)
        for _ in range(60):
            g=mutate(rng,g,self.m.active_features)
            self.assertIsInstance(Genome.from_dict(g.to_dict(),self.m.active_features),Genome)

    def test_seed_determinism_and_sealed_outer_fold(self):
        a=search(self.m,self.lim,SearchConfig(seed=19,population=6,generations=2))
        b=search(self.m,self.lim,SearchConfig(seed=19,population=6,generations=2))
        self.assertEqual(a['champion_sha'],b['champion_sha'])
        self.assertEqual(a['lineage'],b['lineage'])
        self.assertEqual(a['holdout_events'],b['holdout_events'])
        self.assertEqual(a['search']['outer_holdout_reads_during_search'],0)

    def test_limits_stop_at_55pct(self):
        r=simulate(self.m,baseline_hold(self.m.active_features),20,450,Limits(hard_drawdown=.05))
        self.assertLessEqual(r.net_multiple,3)
        self.assertLessEqual(r.max_drawdown,1)

    def test_exclusive_output_and_verified_report(self):
        from argparse import Namespace
        path=self.dir/'out.json'
        args=Namespace(data=str(self.path),output=str(path),source_kind='SYNTHETIC_DEMO',
                       capital=10000.,fee_bps=8.,slippage_bps=7.,max_symbol=.75,
                       max_gross=1.5,halt_dd=.55,seed=11,population=6,generations=1,proposals=None)
        report=run(args)
        self.assertTrue(report['research_only'])
        v=verify(Namespace(data=str(self.path),report=str(path)))
        self.assertEqual(v['status'],'PASS_DETERMINISTIC_REPLAY')
        with self.assertRaises(FileExistsError):run(args)

    def test_planted_leadflow_feature_can_be_discovered(self):
        # Positive software control only: the new synthetic world's *previously
        # emitted* taker imbalance influences the next bar's price path.
        path=self.dir/'leadflow.csv'
        create_demo(path,rows=600,world='leadflow')
        m=load_csv(path,source_type='SYNTHETIC_DEMO')
        x=search(m,self.lim,SearchConfig(seed=77,population=18,generations=4))
        self.assertFalse(x['champion'].get('type')=='CASH')
        self.assertGreater(x['holdout']['net_multiple'],x['holdout_cash']['net_multiple'])
        self.assertGreater(x['holdout']['net_multiple'],1.1)
        self.assertTrue(m.funding_known)
        self.assertIn('flow_imbalance',m.active_features)
        reconciled=reconcile(m,splits(m)['holdout'],self.lim,x['holdout_events'],x['holdout_equity'],x['holdout'])
        self.assertGreater(reconciled['checked_fills'],0)

    def test_tampered_report_detected(self):
        from argparse import Namespace
        p=self.dir/'report.json'
        args=Namespace(data=str(self.path),output=str(p),source_kind='SYNTHETIC_DEMO',
                       capital=10000.,fee_bps=8.,slippage_bps=7.,max_symbol=.75,
                       max_gross=1.5,halt_dd=.55,seed=11,population=6,generations=1,proposals=None)
        run(args)
        raw=json.loads(p.read_text());raw['result']['holdout']['net_multiple']+=.5
        p.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError,'REPORT_MANIFEST_CHANGED'):
            verify(Namespace(data=str(self.path),report=str(p)))


if __name__=='__main__':unittest.main()
