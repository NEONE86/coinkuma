"""Independent stdlib replay comparison and SQLite projection audit. No engine import."""
import argparse,hashlib,json,sqlite3,subprocess,sys
from pathlib import Path

BASE_HASH='34df153cb590ea03fce2845b2e2d5556350f1131ddab1e53f3087116636d1977'
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(b):return hashlib.sha256(b).hexdigest()
def projection(root):
    rows=[json.loads(s) for s in (root/'ledger.jsonl').read_text().splitlines()];latest={};overall=None
    for row in rows:
        b=json.loads(row['body'])
        if row['kind']=='input' and b['source']=='bar':
            raw=read(root/'raw'/(b['sha256']+'.bin'))
            if 'stream' in raw:raw=raw['data']
            logical=(raw['k']['t'],raw['k']['T']);s=raw['s']
            if s not in latest or logical>latest[s][0]:
                latest[s]=(logical,b['id']);overall=(logical,b['id'])
    if overall:latest['*']=overall
    with sqlite3.connect((root/'observer.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        stored=[dict(zip(('seq','kind','id','body','hash','prev'),row)) for row in db.execute('SELECT * FROM records ORDER BY seq')]
        assert stored==rows,'SQLITE_LEDGER_MISMATCH'
        index={s:(kind,key) for s,kind,key in db.execute("SELECT i.symbol,r.kind,r.id FROM input_index i JOIN records r ON i.seq=r.seq WHERE i.source='bar'")}
        assert index=={s:('input',v[1]) for s,v in latest.items()},'BAR_INDEX_LOGICAL_TIME_REGRESSION'
    return {s:v[1] for s,v in latest.items()}

def verify(before,after):
    results={};source_hash=None;verifier=Path(__file__).with_name('verify_sentinel_corrections.py')
    for case in ('control','delayed','ordinary','other_symbol'):
        a=read(before/case/'fixture.json');b=read(after/case/'fixture.json');assert a==b,'FIXTURE_CHANGED'
        data_hash=sha(json.dumps(a,sort_keys=True).encode())
        for version,root in [('before',before),('after',after)]:
            result=read(root/case/'result.json');assert result['fixture_sha256']==data_hash,'FIXTURE_HASH'
            rows=[json.loads(s) for s in (root/case/'export/ledger.jsonl').read_text().splitlines()]
            bodies=[(r['kind'],json.loads(r['body'])) for r in rows]
            contract=bodies[0][1]
            if version=='before':assert contract['source_hash']==BASE_HASH,'BEFORE_SOURCE'
            else:
                source_hash=source_hash or contract['source_hash'];assert contract['source_hash']==source_hash,'AFTER_SOURCE_MIXED'
            receipts=[v for k,v in bodies if k=='receipt'];inputs=[r for r in a if r['method']=='ingest']
            assert len(receipts)==len(inputs),'FIXTURE_RECEIPT_COUNT'
            for receipt,entry in zip(receipts,inputs):
                assert receipt['source']==entry['source'] and receipt['received_at']==entry['at'] and receipt['timing']==entry['timing'],'FIXTURE_RECEIPT_BINDING'
                assert receipt['sha256']==sha(json.dumps(entry['value'],sort_keys=True).encode()),'FIXTURE_ORIGINAL_BYTES'
            decision=[v for k,v in bodies if k=='decision'][-1]
            assert (result['action'],result['reason'])==(decision['action'],decision['reason']),'RESULT_DECISION_BINDING'
            failed=version=='before' and case in ('delayed','ordinary')
            assert (decision['action'],decision['reason'])==(('NO_ACTION','LIQUIDITY_INELIGIBLE') if failed else ('ENTER','ACCESS_HYPOTHESIS_ELIGIBLE')),'BEFORE_AFTER_BEHAVIOR'
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(verifier),str(root/case/'export')],capture_output=True,text=True,encoding='utf-8')
            assert (run.returncode!=0 and 'BAR_LOGICAL_TIME_REGRESSION' in run.stderr) if failed else run.returncode==0,run.stderr
            if not failed:projection(root/case/'export')
            results[version+'/'+case]=dict(action=decision['action'],reason=decision['reason'],fixture_sha256=data_hash,independent_verification='EXPECTED_BASELINE_REGRESSION_REJECTED' if failed else 'PASS')
    return dict(status='PASS_SAME_DATA_BEFORE_AFTER',base_source_hash=BASE_HASH,final_source_hash=source_hash,cases=results,classification='SYNTHETIC_OFFLINE',investment_edge_validated=False)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True);a=p.parse_args()
    print(json.dumps(verify(a.before,a.after),indent=2))
