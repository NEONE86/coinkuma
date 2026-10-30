"""Rehash all links so failure must be semantic, not a stale chain hash."""
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(exist_ok=False)
    controls=[('release_backdated','release_tick','RELEASE_AVAILABILITY'),('initial_deleted','release_tick','PENDING_INITIAL_MISSING'),('terminal_overwritten','exit_structure','RETRY_ORIGINAL_OVERWRITTEN')];results={}
    for name,case,expected in controls:
        dest=a.out/name;shutil.copytree(a.evidence/case/'export',dest)
        rows=[json.loads(s) for s in (dest/'ledger.jsonl').read_text().splitlines()];kept=[];changed=False
        for row in rows:
            b=json.loads(row['body']);kind=row['kind']
            if not changed:
                if name=='release_backdated' and kind=='interpretation_release':b['available_at']=b['received_at'];changed=True
                elif name=='initial_deleted' and kind=='interpretation':changed=True;continue
                elif name=='terminal_overwritten' and kind=='execution' and b.get('reason')=='NEW_MONOTONIC_EPOCH':b['at']-=.001;changed=True
            row['body']=canonical(b);kept.append(row)
        assert changed
        previous='0'*64
        for i,row in enumerate(kept,1):
            row['seq']=i;row['prev']=previous;row['hash']=hashlib.sha256(canonical([previous,row['kind'],row['id'],json.loads(row['body'])]).encode()).hexdigest();previous=row['hash']
        (dest/'ledger.jsonl').write_text(''.join(canonical(r)+'\n' for r in kept),encoding='utf-8')
        run=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).with_name('verify_sentinel_corrections.py')),str(dest)],capture_output=True,text=True,encoding='utf-8')
        (dest/'rejection.txt').write_text(run.stderr,encoding='utf-8');assert run.returncode and expected in run.stderr,(name,run.stderr)
        results[name]=dict(status='REJECTED_REHASHED_TAMPER',reason=expected,classification='SYNTHETIC_NEGATIVE_CONTROL_NOT_MARKET')
    (a.out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(results,indent=2))
if __name__=='__main__':main()
