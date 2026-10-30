"""Independent, standard-library delivery verifier. No implementation imports.

Usage: python -B scripts/verify_observer_followup_bundle.py EXTRACTED_BUNDLE
Verifies manifests, exact execution bytes, immutable original, all positive and
rehashed negative cases, final-version load timing arithmetic and preservation.
"""
import argparse,hashlib,json,sqlite3,statistics,subprocess,sys
from pathlib import Path

def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def main():
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args();root=a.bundle.resolve()
    manifest=read(root/'manifest.json')
    for name,h in manifest.items():assert sha((root/name).read_bytes())==h,('MANIFEST',name)
    identity=read(root/'identity.json');source=root/'source';h=hashlib.sha256()
    for f in sorted((source/'one_sentinel').glob('*.py')):h.update(f.name.encode());h.update(f.read_bytes())
    assert h.hexdigest()==identity['source_hash'],'EXACT_SOURCE'
    for name in ('policy','account'):assert sha(canonical(read(source/'configs'/('sentinel_'+name+'_v1.json'))).encode())==identity[name+'_hash'],'CONFIG_HASH'
    for name,info in read(root/'source_file_hashes.json').items():
        raw=(source/name).read_bytes();assert sha(raw)==info['sha256'],'SOURCE_FILE_HASH'
        def blob(b):return hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
        assert info['git_blob'] in (blob(raw),blob(raw.replace(b'\r\n',b'\n'))),'GIT_CHECKOUT_CONTENT'
    expected=read(root/'preservation/original_files.json')
    actual=root/'original/actual'
    assert len(expected)==333 and {f.relative_to(actual).as_posix():sha(f.read_bytes()) for f in actual.rglob('*') if f.is_file()}==expected,'ORIGINAL_333'
    before=read(root/'preservation/before.json');after=read(root/'preservation/after.json')
    preserved={}
    for name in ('batch','alpha','legacy','dual'):
        old=before[name]['rows'];new=after[name]['rows'];assert new[:len(old)]==old and all(r['body_hash_valid'] for r in old),'RESEARCH_PREFIX'
        preserved[name]=dict(original_rows=len(old),new_rows=len(new)-len(old))
    for key in ('live_head','live_status','live_tracked_files'):assert before[key]==after[key],('LIVE_PRESERVATION',key)
    authority_usage={}
    assert len(before['authority_engine_budgets'])==len(after['authority_engine_budgets']),'AUTHORITY_ENGINES'
    for old,new in zip(before['authority_engine_budgets'],after['authority_engine_budgets']):
        # Existing live work continues independently. Usage/reservations are not
        # immutable limits; retain and report their observed changes explicitly.
        for key in ('engine','cash_limit','time_limit','cash_spent','cash_reserved','engine_halted','halt_reason'):assert old[key]==new[key],('AUTHORITY_LIMIT_OR_CASH',key)
        assert new['time_spent']>=old['time_spent'],'AUTHORITY_TIME_REGRESSION'
        authority_usage[old['engine']]={k:dict(before=old[k],after=new[k]) for k in ('time_spent','time_reserved')}
    results={}
    verifier=source/'scripts/verify_sentinel_corrections.py'
    for group,expected_count in (('legacy',7),('followup',11)):
        dirs=sorted(d for d in (root/'evidence'/group).iterdir() if d.is_dir());assert len(dirs)==expected_count,'CASE_COUNT'
        for d in dirs:
            ledger=[json.loads(s) for s in (d/'export/ledger.jsonl').read_text().splitlines()]
            contract=json.loads(ledger[0]['body']);assert all(contract[k]==identity[k] for k in ('source_hash','policy_hash','account_hash')),'CASE_EXACT_VERSION'
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(d/'export')],capture_output=True,text=True,encoding='utf-8')
            assert run.returncode==0,(group,d.name,run.stderr)
            results[group+'/'+d.name]=json.loads(run.stdout)['status']
    negatives={}
    for group,n in (('legacy',4),('followup',3)):
        controls=read(root/'tamper'/group/'results.json');assert len(controls)==n,'TAMPER_COUNT'
        for name,info in controls.items():
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(root/'tamper'/group/name)],capture_output=True,text=True,encoding='utf-8')
            assert run.returncode and info['reason'] in run.stderr,('TAMPER_NOT_REJECTED',name,run.stderr)
            negatives[group+'/'+name]=info['reason']
    load=root/'load/final_1h';result=read(load/'result.json');samples=[json.loads(s) for s in (load/'service_samples.jsonl').read_text().splitlines()]
    assert result['source_hash']==identity['source_hash'],'LOAD_EXACT_SOURCE'
    policy=read(load/'SYNTHETIC_ONLY_policy.json');assert sha(canonical(policy).encode())==result['policy_hash'],'LOAD_FIXTURE_POLICY'
    frozen=read(source/'configs/sentinel_policy_v1.json');copy=dict(policy);copy.pop('fixture_only');copy['proposed_new_trial_limits']=dict(copy['proposed_new_trial_limits'],raw_bytes_per_day=result['production_cap_unchanged']);assert copy==frozen,'LOAD_CHANGED_OTHER_RULES'
    assert len(samples)==3912 and result['milestones'][-1]['simulated_seconds']==3600,'LOAD_SIZE'
    timings=[s['service_seconds'] for s in samples];last=result['milestones'][-1]
    assert abs(statistics.mean(timings)*1000-last['mean_ingest_ms'])<1e-8 and sorted(timings)[int(.95*(len(timings)-1))]*1000==last['p95_ingest_ms'],'LOAD_TIMING_ARITHMETIC'
    assert sum(s['bytes'] for s in samples)==last['cumulative_received_bytes']==last['charged_rolling_24h_bytes'],'LOAD_ACCOUNTING'
    chain=hashlib.sha256();backlog=0;peak=0;previous=None
    with sqlite3.connect((load/'runtime/observer.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        for sample in samples:
            row=db.execute("SELECT body FROM records WHERE kind='receipt' AND id=?",(sample['receipt_id'],)).fetchone();assert row,'LOAD_RECEIPT'
            body=json.loads(row[0]);assert body['source']==sample['source'] and body['received_at']==sample['at'] and body['bytes']==sample['bytes'] and body['classification']=='SYNTHETIC','LOAD_SAMPLE_BINDING'
            assert db.execute("SELECT 1 FROM records WHERE kind='input' AND id=?",(sample['receipt_id'],)).fetchone(),'LOAD_NOT_ACCEPTED'
            raw=(load/'runtime/raw'/(body['sha256']+'.bin')).read_bytes();assert sha(raw)==body['sha256'] and len(raw)==body['bytes'],'LOAD_RAW'
            chain.update(sample['receipt_id'].encode())
            if previous is not None:backlog=max(0,backlog-(sample['at']-previous))
            backlog+=sample['service_seconds'];peak=max(peak,backlog);previous=sample['at']
    assert chain.hexdigest()==last['receipt_id_digest'] and abs(peak-last['max_simulated_backlog_seconds'])<1e-8,'LOAD_SEQUENCE_BACKLOG'
    draft=read(root/'NEXT_5MIN_APPROVAL_DRAFT.json');assert all(draft[k] is None for k in ('start_at','stop_at','approval_reference')),'DRAFT_ACTIVATED'
    assert all(draft[k]==identity[k] for k in ('commit','source_hash','policy_hash','account_hash')),'DRAFT_VERSION'
    assert (draft['requested_seconds'],draft['trial_received_byte_cap'],draft['trial_http_attempt_cap'],draft['trial_book_attempt_cap'],draft['model_cash_budget'],draft['live_orders'],draft['scheduler_changes'])==(300,67108864,12,6,0,False,False),'DRAFT_LIMITS'
    print(json.dumps(dict(status='PASS_OFFLINE_ONLY',identity=identity,positive_cases=results,negative_controls=negatives,original_files=333,preserved_research=preserved,existing_live_authority_usage=authority_usage,load=dict(accepted_inputs=len(samples),p95_ms=last['p95_ingest_ms'],classification='SYNTHETIC_ACCELERATED_1H'),live_public_trial='NOT_RUN_NOT_APPROVED',investment_edge_validated=False),indent=2))
if __name__=='__main__':main()
