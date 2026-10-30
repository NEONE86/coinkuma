"""Negative controls: rehashed false terminal reason; stale derived bar index."""
import argparse,hashlib,json,shutil,sqlite3,subprocess,sys
from pathlib import Path
from verify_bar_order import projection
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def main():
    p=argparse.ArgumentParser();p.add_argument('--after',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(exist_ok=False);results={}
    for name in ('terminal_reason','derived_index'):
        root=a.out/name;shutil.copytree(a.after/'delayed/export',root)
        if name=='terminal_reason':
            rows=[json.loads(s) for s in (root/'ledger.jsonl').read_text().splitlines()];changed=0;prev='0'*64
            for row in rows:
                b=json.loads(row['body'])
                if row['kind']=='clock_pending_result' and b['reason']=='OUT_OF_ORDER_BAR_TIME':b['reason']='DUPLICATE_BAR';changed+=1
                row['body']=canonical(b);row['prev']=prev;row['hash']=hashlib.sha256(canonical([prev,row['kind'],row['id'],b]).encode()).hexdigest();prev=row['hash']
            assert changed==1
            (root/'ledger.jsonl').write_text(''.join(canonical(r)+'\n' for r in rows),encoding='utf-8')
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).with_name('verify_sentinel_corrections.py')),str(root)],capture_output=True,text=True,encoding='utf-8')
            assert run.returncode and 'BAR_TERMINAL_REASON' in run.stderr,run.stderr
            (root/'rejection.txt').write_text(run.stderr,encoding='utf-8');reason='BAR_TERMINAL_REASON'
        else:
            db=sqlite3.connect(root/'observer.sqlite3')
            try:
                seq=next(seq for seq,body in db.execute("SELECT seq,body FROM records WHERE kind='receipt' ORDER BY seq") if json.loads(body)['source']=='bar')
                db.execute("UPDATE input_index SET seq=? WHERE source='bar' AND symbol='NEWUSDT'",(seq,));db.commit()
            finally:db.close()
            try:projection(root)
            except AssertionError as exc:assert str(exc)=='BAR_INDEX_LOGICAL_TIME_REGRESSION';reason=str(exc)
            else:raise AssertionError('TAMPER_NOT_REJECTED')
            (root/'rejection.txt').write_text(reason,encoding='utf-8')
        results[name]=dict(status='REJECTED',reason=reason,classification='SYNTHETIC_NEGATIVE_CONTROL')
    (a.out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(results,indent=2))
if __name__=='__main__':main()
