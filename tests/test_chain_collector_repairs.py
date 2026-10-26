"""COL-01..04 regressions, synthetic diagnostics only."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_chain_collector as fixtures
from test_chain_collector import Response, fixture, topic, addr, word
from one_trading.economic_research import chain_cycle as cc
from one_trading.economic_research.evidence import VersionedEvidence
from one_trading.economic_research.events import register_asset
from one_trading.economic_research.store import Store, canonical, digest
from one_trading.economic_research.theses import make_packet, validate_packet
from scripts.recompute_chain_cycle import recompute


class CollectorRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);self.addCleanup(self.store.close)
        self.case=fixtures.CollectorTests();self.case.store=self.store
        self.case.spec,self.case.block,self.case.receipts=fixture()
        self.asset=self.register("UNI",self.case.spec["addresses"]["uni"])
        self.other=self.register("AAVE",addr(99))

    def register(self, key, contract):
        evidence=VersionedEvidence(self.store)
        url="https://raw.githubusercontent.com/synthetic-review/fixture/"+key
        res=Response(json.dumps(dict(address=contract)).encode());res.url=url
        evidence.opener.open=lambda *a,**k:res
        _,receipt=evidence.get(url)
        register_asset(self.store,dict(asset_id=key,symbol=key,chain_id="eip155:1",contract_address=contract,
            economic_asset_id=key,exchange_symbol_mapping={"TEST":dict(instrument=key,quote_currency="USDT",
            evidence_ids=[receipt["snapshot_id"]],mapping_basis="SYNTHETIC")},identity_evidence_ids=[receipt["snapshot_id"]],
            decimals=18,valid_from="2020-01-01T00:00:00Z",valid_to=None,migration_evidence_ids=[],representation="CANONICAL"))
        return key

    def cycle(self, mutation=None):
        return cc.collect_cycle(self.store,self.case.spec,self.case.capture(mutation))

    def domains(self):
        return {k:digest(self.store.all(k)) for k in ("observation","chain_fee_fact","economic_event_revision","evidence_packet","collection_link")}

    def test_col01_failed_proofs_never_promote_candidates(self):
        for name in ("control","reorg","not_finalized","wrong_route","wrong_units"):
            with self.subTest(name=name):
                if name != "control": self.setUp()
                # Independent fresh spec IDs preserve every candidate capture.
                self.case.spec["spec_id"]=name
                def mutate(method,params,result):
                    if name=="reorg" and method=="eth_getBlockByNumber" and params[1] is True:result["hash"]=word(8)
                    if name=="not_finalized" and method=="eth_getBlockByNumber" and params[0]=="finalized":result["number"]="0x0"
                    if name=="wrong_route" and method=="eth_call" and params[0]["data"]=="0x490c98f5":result=topic(addr(999))
                    if name=="wrong_units" and method=="eth_call" and params[0]["data"]=="0x313ce567":result=word(37)
                    return result
                c=self.cycle(mutate);before=len(self.store.all("chain_fee_fact"))
                link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
                event=self.store.get("economic_event_revision",link["event_revision_id"])
                if name=="control":
                    self.assertEqual(event["status"],"EXECUTED")
                    self.assertEqual(c["measurement"]["qualified_fee_count"],1)
                else:
                    self.assertNotEqual(event["status"],"EXECUTED")
                    self.assertEqual(len(self.store.all("chain_fee_fact")),before)
                    self.assertEqual(c["measurement"]["observed_qualified_fee_count"],0)
                    self.assertEqual(c["measurement"]["flows"][0]["qualification_status"],"UNVERIFIED")
                    self.assertEqual(c["measurement"]["flows"][0]["attribution"],"UNVERIFIED_CANDIDATE")
                    self.assertIsNone(c["measurement"]["qualified_fee_count"])

    def test_col01_missing_other_receipt_page_keeps_valid_positive_lower_bound(self):
        self.case.spec.update(start_block=0,start_hash=word(0),start_utc="2019-12-31T23:59:48Z")
        def mutate(method,params,result):
            if method=="eth_getBlockByNumber" and params[0]=="0x0":
                return dict(number="0x0",hash=word(0),parentHash=word(0),timestamp=hex(1577836788),transactions=[])
            if method=="eth_getBlockReceipts" and params==["0x0"]:return None
            return result
        c=self.cycle(mutate);m=c["measurement"]
        self.assertEqual(m["status"],"DATA_MISSING");self.assertIsNone(m["qualified_fee_count"])
        self.assertEqual(m["observed_qualified_fee_count"],1);self.assertFalse(m["zero_proven"])
        link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
        self.assertEqual(self.store.get("economic_event_revision",link["event_revision_id"])["status"],"EXECUTED")
        self.assertEqual(len(self.store.all("chain_fee_fact")),1)
        self.assertEqual(recompute(Path(self.temp.name),self.case.spec,c)["verification"],"PASS")

    def test_partial_identity_replay_uses_receipt_order_after_json_key_sorting(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                if legacy:self.setUp()
                def fail_hook(method,params,result):
                    if method=="eth_getCode" and params[0]==self.case.spec["addresses"]["hook"]:return None
                    return result
                if legacy:
                    with patch.object(cc,"measure",side_effect=cc.measure_legacy):c=self.cycle(fail_hook)
                else:c=self.cycle(fail_hook)
                before=self.store.verify()
                m=cc.validated_measurement(self.store,c)
                self.assertFalse(m["identity_verified"])
                self.assertEqual(cc.collect_cycle(self.store,self.store.get("collection_spec",self.case.spec["spec_id"])),c)
                self.assertEqual(before,self.store.verify())

    def test_col02_wrong_asset_before_and_after_link_has_no_domain_writes(self):
        c=self.cycle();before=self.domains()
        with self.assertRaisesRegex(ValueError,"collection_asset_identity"):cc.link_cycle(self.store,c["cycle_id"],self.other)
        self.assertEqual(self.domains(),before)
        link=cc.link_cycle(self.store,c["cycle_id"],self.asset);before=self.domains()
        with self.assertRaisesRegex(ValueError,"collection_asset_identity"):cc.link_cycle(self.store,c["cycle_id"],self.other)
        self.assertEqual(self.domains(),before)
        self.assertEqual(cc.link_cycle(self.store,c["cycle_id"],self.asset),link)
        self.assertEqual(self.domains(),before)

    def test_col02_registry_chain_representation_and_units_are_bound(self):
        c=self.cycle();base=self.store.get("asset_identity",self.asset)
        for i,(field,value) in enumerate((("chain_id","eip155:56"),("representation","BRIDGED"),("decimals",6),("decimals",True))):
            body=dict(base,asset_id="bad"+str(i));body[field]=value
            self.store.put("asset_identity",body["asset_id"],body)
            before=self.domains()
            with self.assertRaises(ValueError):cc.link_cycle(self.store,c["cycle_id"],body["asset_id"])
            self.assertEqual(self.domains(),before)

    def test_col02_existing_link_validates_stored_target(self):
        c=self.cycle();link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
        original=self.store.get
        def changed(kind,key,optional=False):
            value=original(kind,key,optional)
            return dict(value,asset_id=self.other) if kind=="observation" and key==link["observation_id"] else value
        with patch.object(self.store,"get",side_effect=changed):
            with self.assertRaisesRegex(ValueError,"collection_link_target"):cc.link_cycle(self.store,c["cycle_id"],self.asset)

    def test_col03_atomic_rollback_at_event_and_packet_boundaries(self):
        c=self.cycle()
        for boundary in ("register_event","make_packet"):
            original=getattr(cc,boundary);before=self.domains()
            def fail(*a,**k):original(*a,**k);raise RuntimeError("after persisted boundary")
            with patch.object(cc,boundary,side_effect=fail):
                with self.assertRaises(RuntimeError):cc.link_cycle(self.store,c["cycle_id"],self.asset)
            self.assertEqual(self.domains(),before)
        link=cc.link_cycle(self.store,c["cycle_id"],self.asset);before=self.domains()
        self.assertEqual(cc.link_cycle(self.store,c["cycle_id"],self.asset),link)
        self.assertEqual(self.domains(),before)
        event=self.store.get("economic_event_revision",link["event_revision_id"])
        self.assertNotIn(event["event_id"],event["related_event_ids"])

    def test_col03_reuses_checked_old_nonatomic_event_and_packet(self):
        c=self.cycle();original=cc.make_packet
        def fail(*a,**k):original(*a,**k);raise RuntimeError("legacy nonatomic interruption")
        with patch.object(self.store,"transaction",return_value=nullcontext()),patch.object(cc,"make_packet",side_effect=fail):
            with self.assertRaises(RuntimeError):cc.link_cycle(self.store,c["cycle_id"],self.asset)
        old={k:copy.deepcopy(self.store.all(k)) for k in ("observation","chain_fee_fact","economic_event_revision","evidence_packet")}
        link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
        self.assertEqual(link["packet_id"],old["evidence_packet"][0]["packet_id"])
        for k,rows in old.items():self.assertEqual(self.store.all(k),rows)
        self.assertEqual(cc.link_cycle(self.store,c["cycle_id"],self.asset),link)

    def test_col03_legacy_v1_partial_rows_recover_without_rewriting(self):
        for packet_persisted in (False,True):
            with self.subTest(packet_persisted=packet_persisted):
                if packet_persisted:self.setUp()
                with patch.object(cc,"measure",side_effect=cc.measure_legacy):c=self.cycle()
                spec=self.case.spec; m=c["measurement"]
                sources=sorted({r["snapshot_id"] for r in m["requests"].values() if r.get("snapshot_id")})
                oid=cc.identifier("chain-observation",[c["cycle_id"],self.asset])
                cc.observe(self.store,dict(observation_id=oid,asset_id=self.asset,metric="bounded_hook_fee_collection_progress",
                    value="Collection COMPLETE; reconciled fee logs=1; causal increment AMBIGUOUS",value_type="TEXT",
                    unit="COLLECTION_PROGRESS",period_start=spec["start_utc"],period_end=spec["end_utc"],epistemic_type="SOURCE_CLAIM",
                    evidence_ids=sources,locator=dict(snapshot_id=sources[0],json_pointer="/jsonrpc"),
                    derivation_version="bounded-ethereum-fee-cycle-v1",collection_cycle_id=c["cycle_id"],missing_reason=None))
                from one_trading.economic_research.chain_qualification import fact_key
                for f in m["flows"]:
                    fid=fact_key(spec,f)
                    self.store.put("chain_fee_fact",fid,dict(f,fact_id=fid,first_cycle_id=c["cycle_id"],synthetic=True,available_at=c["completed_at"]))
                eid=cc.identifier("collection-event",spec)
                cc.register_event(self.store,dict(event_id=eid,revision_id=eid+"-r1",asset_id=self.asset,
                    mechanism_id="VALUE_CAPTURE_CHANGE",status="EXECUTED",effective_at=spec["end_utc"],published_at=None,
                    decision_authority="Frozen read-only collector; no trading authority",observation_ids=[oid],related_event_ids=[],
                    economic_flow_ids=[],independent_cluster_id="UNI-aggregator-hook-collection",supersedes_revision_id=None))
                if packet_persisted:make_packet(self.store,[self.asset])
                before=[dict(r) for r in self.store.db.execute("select * from records order by sequence")]
                link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
                self.assertEqual([dict(r) for r in self.store.db.execute("select * from records order by sequence")][:len(before)],before)
                self.assertEqual(len(self.store.all("economic_event_revision")),1)
                self.assertEqual(len(self.store.all("chain_fee_fact")),1)
                self.assertEqual(len(self.store.all("evidence_packet")),1)
                before=self.domains();self.assertEqual(cc.link_cycle(self.store,c["cycle_id"],self.asset),link)
                self.assertEqual(before,self.domains())

    def test_col04_full_semantics_rejected_even_if_corrupted_json_matches_database(self):
        sink=copy.deepcopy(self.case.receipts[0]["logs"][2]);sink.update(logIndex="0x3",data=word(2*10**18))
        sink["topics"][1]=topic(self.case.spec["addresses"]["firepit"]);sink["topics"][2]=topic(self.case.spec["uni_sink"])
        self.case.receipts[0]["logs"].append(sink)
        c=self.cycle();self.assertEqual(recompute(Path(self.temp.name),self.case.spec,c)["verification"],"PASS")
        changes=[]
        for key,value in (("block_hash",word(999)),("block_number",999),("effective_at","1999-01-01T00:00:00+00:00"),
                          ("json_pointer","/result/99/logs/99"),("receipt_snapshot_id","wrong"),("block_snapshot_id","wrong")):
            bad=copy.deepcopy(c);bad["measurement"]["flows"][0][key]=value;changes.append(bad)
        bad=copy.deepcopy(c);bad["measurement"]["shared_uni_sink_transfers"][0]["raw_amount"]=str(999999999*10**18);changes.append(bad)
        bad=copy.deepcopy(c);bad["measurement"]["shared_uni_sink_transfers"][0]["firepit_release_log_present"]=True;changes.append(bad)
        bad=copy.deepcopy(c);bad["measurement"]["hook_swaps"][0]["transaction_hash"]=word(999);changes.append(bad)
        # Deliberately corrupt a disposable export. Production append-only
        # triggers stay intact in the source ledger.
        forged_root=Path(self.temp.name)/"forged-export"
        forged_root.mkdir()
        shutil.copytree(Path(self.temp.name)/"evidence",forged_root/"evidence")
        forged_db=sqlite3.connect(forged_root/"research.sqlite3")
        self.addCleanup(forged_db.close)
        forged_db.execute("CREATE TABLE records(kind TEXT,id TEXT,body TEXT,sha256 TEXT)")
        forged_db.executemany("INSERT INTO records VALUES(?,?,?,?)",self.store.db.execute("SELECT kind,id,body,sha256 FROM records"))
        for bad in changes:
            with self.assertRaises(AssertionError):recompute(Path(self.temp.name),self.case.spec,bad)
            # Explicitly corrupted synthetic database, including a fresh body
            # checksum, proves raw reconstruction is not replaced by DB equality.
            forged_db.execute("UPDATE records SET body=?,sha256=? WHERE kind='collection_cycle' AND id=?",
                (canonical(bad).decode(),digest(bad),c["cycle_id"]))
            forged_db.commit()
            with self.assertRaises(AssertionError):recompute(forged_root,self.case.spec,bad)

    def test_legacy_bad_event_is_reviewed_without_rewriting_history(self):
        def reorg(method,params,result):
            if method=="eth_getBlockByNumber" and params[1] is True:result["hash"]=word(8)
            return result
        c=self.cycle(reorg);forged=copy.deepcopy(c["measurement"])
        forged["validation"]["event_status"]="EXECUTED"
        forged["flows"][0]["qualification_status"]="QUALIFIED"
        with patch.object(cc,"validated_measurement",return_value=forged):link=cc.link_cycle(self.store,c["cycle_id"],self.asset)
        before=[dict(r) for r in self.store.db.execute("select * from records order by sequence")]
        review=cc.review_cycle(self.store,c["cycle_id"])
        self.assertEqual(review["status"],"QUARANTINED_LINK")
        self.assertEqual(review["excluded_event_revision_ids"],[link["event_revision_id"]])
        self.assertEqual([dict(r) for r in self.store.db.execute("select * from records order by sequence")][:len(before)],before)
        validate_packet(self.store,self.store.get("evidence_packet",link["packet_id"]))
        with self.assertRaisesRegex(ValueError,"no_available_event"):make_packet(self.store,[self.asset])
        count=self.store.verify();self.assertEqual(cc.review_cycle(self.store,c["cycle_id"]),review);self.assertEqual(count,self.store.verify())


if __name__=="__main__":unittest.main()
