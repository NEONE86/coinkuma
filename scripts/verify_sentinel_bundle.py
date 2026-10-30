"""Offline standard-library review-bundle verification. No implementation imports.

Hashes attest internal consistency, not an independent trusted timestamp. Source
and results are supplied for external review; synthetic checks prove no alpha.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def ledger(path):return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def verify(root):
    manifest=read(root/'manifest.json')
    for name,entry in manifest.items():
        path=(root/name).resolve()
        assert path.is_relative_to(root),'MANIFEST_ESCAPE'
        raw=path.read_bytes()
        assert len(raw)==entry['bytes'] and sha(raw)==entry['sha256'],f'FILE_HASH: {name}'
    ident=read(root/'identity.json');h=hashlib.sha256()
    for path in sorted((root/'source/one_sentinel').glob('*.py')):
        h.update(path.name.encode());h.update(path.read_bytes())
    assert h.hexdigest()==ident['source_hash'],'SOURCE_BYTES'
    for kind in ('policy','account'):
        value=read(root/f'source/configs/sentinel_{kind}_v1.json')
        assert sha(canonical(value).encode())==ident[f'{kind}_hash'],'CONFIG_CANONICAL_HASH'
    old=ledger(root/'original/actual/ledger.jsonl')
    old_receipts={row['id']:json.loads(row['body']) for row in old if row['kind']=='receipt'}
    results={}
    verifier=root/'source/scripts/verify_sentinel_corrections.py'
    for case in sorted((root/'evidence').iterdir()):
        if not case.is_dir():continue
        export=case/'export';rows=ledger(export/'ledger.jsonl')
        contract=json.loads(rows[0]['body'])
        assert contract['source_hash']==ident['source_hash'],'CASE_SOURCE_VERSION'
        assert contract['policy_hash']==ident['policy_hash'] and contract['account_hash']==ident['account_hash'],'CASE_CONFIG'
        with closing(sqlite3.connect((export/'observer.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            assert [dict(r) for r in db.execute('select * from records order by seq')]==rows,'SQLITE_EXPORT'
        call=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(export)],capture_output=True,text=True,encoding='utf-8')
        assert call.returncode==0,case.name+': '+call.stderr
        results[case.name]=read(case/'verification_stdout.txt')
        assert json.loads(call.stdout)==results[case.name],'SAVED_INDEPENDENT_RESULT'
        if case.name=='real_reused':
            receipts=[json.loads(row['body']) for row in rows if row['kind']=='receipt']
            assert len(receipts)==len(old_receipts)==326,'REPLAY_COUNT'
            for r in receipts:
                p=r['provenance'];original=old_receipts[p['original_receipt_id']]
                assert r['classification']=='REAL_REUSED' and r['received_at']==original['received_at'],'REPLAY_TIME'
                assert r['sha256']==original['sha256']==p['original_sha256'] and r['origin']==original['origin'],'REPLAY_ORIGIN'
                assert (export/'raw'/(r['sha256']+'.bin')).read_bytes()==(root/'original/actual/raw'/(r['sha256']+'.bin')).read_bytes(),'REPLAY_RAW'
            assert results[case.name]['item_states']=={'UNVERIFIABLE':35999},'NO_RETROACTIVE_CALIBRATION'
    before=read(root/'preservation/before.json');after=read(root/'preservation/after.json')
    preservation={}
    for name in ('alpha','legacy','batch','dual'):
        a=before[name]['rows'];b=after[name]['rows']
        assert b[:len(a)]==a and all(r['body_hash_valid'] for r in b),f'PRESERVED_PREFIX: {name}'
        preservation[name]=dict(original_rows=len(a),after_rows=len(b),original_prefix_unchanged=True)
    assert before['authority_engine_budgets']==after['authority_engine_budgets'],'AUTHORITY_BUDGET'
    assert before['live_head']==after['live_head'] and before['live_tracked_files']==after['live_tracked_files'],'LIVE_SOURCE'
    negative={}
    for case in sorted((root/'tamper-checks').iterdir()):
        if not case.is_dir():continue
        run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(case)],capture_output=True,text=True,encoding='utf-8')
        assert run.returncode!=0,'ACCEPTED_TAMPER: '+case.name
        negative[case.name]=run.stderr.strip().splitlines()[-1]
    load=read(root/'load/release/result.json')
    load_source=hashlib.sha256()
    for path in sorted((root/'load/source_at_24h/one_sentinel').glob('*.py')):
        load_source.update(path.name.encode());load_source.update(path.read_bytes())
    assert load['source_hash']==load_source.hexdigest(),'LOAD_EXACT_VERSION'
    assert load['milestones'][0]['simulated_seconds']==3600 and load['milestones'][-1]['simulated_seconds']==86400,'LOAD_COMPLETION'
    assert load['network_calls']==0 and load['status']['inputs']==93888,'LOAD_ACCEPTED_COUNT'
    assert load['production_cap_unchanged']==268435456 and load['fixture_cap']==8589934592,'LOAD_POLICY_SEPARATION'
    assert load['script_sha256']==sha((root/'source/scripts/benchmark_sentinel.py').read_bytes()),'LOAD_GENERATOR'
    final_load=read(root/'load/final_1h/result.json')
    assert final_load['source_hash']==ident['source_hash'] and final_load['milestones'][-1]['simulated_seconds']==3600,'FINAL_SOURCE_1H'
    assert final_load['status']['inputs']==3912 and final_load['network_calls']==0,'FINAL_SOURCE_ACCEPTED'
    proposal=read(root/'NEXT_5MIN_APPROVAL_DRAFT.json')
    assert proposal['status']=='NOT_APPROVED_DO_NOT_EXECUTE' and not proposal.get('approval_reference'),'DRAFT_NOT_AUTHORITY'
    assert all(proposal[k]==ident[k] for k in ('commit','source_hash','policy_hash','account_hash')),'PROPOSAL_VERSION'
    return dict(status='PASS_OFFLINE_BUNDLE',files=len(manifest),cases=list(results),
                preservation=preservation,rehashed_tamper_rejected=negative,
                new_public_collection=0,actual_orders=0,paid_model_calls=0,actual_channel_sends=0,
                investment_edge_validated=False,
                limits='Hash/prefix checks do not attest external time, private runtime bodies are not distributed, accelerated load is not live operation.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args()
    print(json.dumps(verify(a.bundle.resolve()),ensure_ascii=False,indent=2))
