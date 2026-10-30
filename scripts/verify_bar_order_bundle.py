"""Independent standard-library audit of the bar-order correction delivery."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
from verify_bar_order import verify,projection

def read(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(b):return hashlib.sha256(b).hexdigest()
def digest(x):return sha(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
def source_hash(root):
    h=hashlib.sha256()
    for f in sorted((root/'one_sentinel').glob('*.py')):h.update(f.name.encode());h.update(f.read_bytes())
    return h.hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args();root=a.bundle.resolve();identity=read(root/'identity.json')
    for name,expected in read(root/'manifest.json').items():assert sha((root/name).read_bytes())==expected,('MANIFEST',name)
    assert source_hash(root/'source')==identity['source_hash'],'EXACT_FINAL_BYTES'
    assert source_hash(root/'baseline-source')==identity['base_source_hash'],'EXACT_BASE_BYTES'
    for name in ('policy','account'):
        path='configs/sentinel_'+name+'_v1.json'
        assert (root/'source'/path).read_bytes()==(root/'baseline-source'/path).read_bytes(),'POLICY_OR_ACCOUNT_CHANGED'
        assert digest(read(root/'source'/path))==identity[name+'_hash'],'CONFIG_HASH'
    for name,info in read(root/'source_file_hashes.json').items():
        raw=(root/'source'/name).read_bytes();assert sha(raw)==info['sha256'],'SOURCE_HASH'
        def blob(b):return hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
        assert info['git_blob'] in (blob(raw),blob(raw.replace(b'\r\n',b'\n'))),'GIT_SOURCE_CONTENT'
    original=root/'original/actual';expected=read(root/'preservation/original_files.json')
    assert len(expected)==333 and {p.relative_to(original).as_posix():sha(p.read_bytes()) for p in original.rglob('*') if p.is_file()}==expected,'ORIGINAL_333'
    before=read(root/'preservation/before.json');after=read(root/'preservation/after.json');preserved={}
    for key in ('batch','alpha','legacy','dual'):
        old=before[key]['rows'];new=after[key]['rows'];assert new[:len(old)]==old and all(r['body_hash_valid'] for r in old),'RESEARCH_PRESERVATION'
        preserved[key]=dict(original=len(old),after=len(new))
    for key in ('live_head','live_status','live_tracked_files'):assert before[key]==after[key],('OPERATING_CHECKOUT',key)
    usage={}
    assert len(before['authority_engine_budgets'])==len(after['authority_engine_budgets']),'AUTHORITY_ENGINES'
    for old,new in zip(before['authority_engine_budgets'],after['authority_engine_budgets']):
        for key in ('engine','cash_limit','time_limit','cash_spent','cash_reserved','engine_halted','halt_reason'):assert old[key]==new[key],('AUTHORITY',key)
        assert new['time_spent']>=old['time_spent'],'USAGE_REGRESSION'
        usage[old['engine']]={k:[old[k],new[k]] for k in ('time_spent','time_reserved')}
    comparison=verify(root/'bar-order/before',root/'bar-order/after');assert comparison['final_source_hash']==identity['source_hash'],'MIXED_FINAL_SOURCE'
    positives={};verifier=root/'source/scripts/verify_sentinel_corrections.py'
    for group,n in (('legacy',7),('p1p2',11)):
        cases=[p for p in (root/'evidence'/group).iterdir() if p.is_dir()];assert len(cases)==n,'CASE_COUNT'
        for case in cases:
            export=case/'export';contract=json.loads(json.loads((export/'ledger.jsonl').read_text().splitlines()[0])['body'])
            assert all(contract[k]==identity[k] for k in ('source_hash','policy_hash','account_hash')),'CASE_VERSION'
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(export)],capture_output=True,text=True,encoding='utf-8')
            assert run.returncode==0,run.stderr;projection(export);positives[group+'/'+case.name]='PASS'
    negatives={}
    for group,n in (('legacy',4),('p1p2',3),('bar-order',2)):
        controls=read(root/'tamper'/group/'results.json');assert len(controls)==n,'NEGATIVE_COUNT'
        for name,info in controls.items():
            path=root/'tamper'/group/name
            if name=='derived_index':
                try:projection(path)
                except AssertionError as e:assert str(e)==info['reason']
                else:raise AssertionError('INDEX_TAMPER_ACCEPTED')
            else:
                run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(path)],capture_output=True,text=True,encoding='utf-8')
                assert run.returncode and info['reason'] in run.stderr,('TAMPER_ACCEPTED',group,name,run.stderr)
            negatives[group+'/'+name]=info['reason']
    print(json.dumps(dict(status='PASS_OFFLINE_ONLY',identity=identity,comparison=comparison,preserved_original_files=333,preserved_research=preserved,existing_authority_usage=usage,previous_positive_cases=positives,negative_controls=negatives,public_collection='NOT_RUN_NOT_APPROVED',investment_edge_validated=False),indent=2))
if __name__=='__main__':main()
