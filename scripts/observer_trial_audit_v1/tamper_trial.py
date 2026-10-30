"""Offline negative controls on temporary copies, never on original evidence."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

def main(bundle, temp_parent):
    bundle=bundle.resolve();temp_parent=temp_parent.resolve();temp_parent.mkdir(exist_ok=True,parents=True)
    checks=[]
    for case,expected in [('raw','ORIGINAL_RAW'),('budget','BYTE_CAP'),('approval','APPROVAL_BINDING'),('bar_projection','BAR_INDEX_LOGICAL_TIME_REGRESSION')]:
        with tempfile.TemporaryDirectory(prefix='one-audit-control-',dir=temp_parent) as temporary:
            assert Path(temporary).resolve().is_relative_to(temp_parent)
            copy=Path(temporary)/'bundle'
            # Exclude the final ZIP; only the evidence directory is copied.
            shutil.copytree(bundle,copy)
            def read(p):return json.loads(p.read_text(encoding='utf-8'))
            def write(p,v):p.write_text(json.dumps(v,indent=2),encoding='utf-8')
            if case=='raw':
                rows=[json.loads(s) for s in (copy/'actual/export/ledger.jsonl').read_text(encoding='utf-8').splitlines()]
                first=json.loads(next(r['body'] for r in rows if r['kind']=='receipt'))
                raw=copy/'actual/export/raw'/(first['sha256']+'.bin');raw.write_bytes(raw.read_bytes()+b' ')
            elif case=='budget':
                path=copy/'actual/export/meter.json';v=read(path);v['rolling_24h_upper_bound_charge']=67108865;write(path,v)
            elif case=='approval':
                path=copy/'actual/execution_approval.json';v=read(path);v['trial_http_attempt_cap']=13;write(path,v)
            else:
                with closing(sqlite3.connect(copy/'actual/export/observer.sqlite3')) as db:
                    seq=db.execute("SELECT MIN(seq) FROM records WHERE kind='input' AND json_extract(body,'$.source')='bar' AND json_extract(body,'$.parsed.symbol')='BTCUSDT'").fetchone()[0]
                    assert seq is not None
                    db.execute("UPDATE input_index SET seq=? WHERE source='bar' AND symbol='BTCUSDT'",(seq,));db.commit()
            result=subprocess.run([sys.executable,'-B','-X','utf8',str(copy/'tools/verify_trial.py'),str(copy)],capture_output=True,text=True,encoding='utf-8')
            assert result.returncode!=0 and expected in result.stderr,(case,result.returncode,result.stderr)
            checks.append(dict(case=case,status='EXPECTED_REJECTION',reason=expected,returncode=result.returncode))
    return dict(status='PASS_FOUR_OFFLINE_TAMPER_CONTROLS',classification='OFFLINE_TAMPER_TEST_NOT_NEW_MARKET_DATA',checks=checks)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);p.add_argument('--temp-parent',required=True,type=Path);p.add_argument('--out',type=Path);a=p.parse_args()
    result=main(a.bundle,a.temp_parent);text=json.dumps(result,indent=2)
    if a.out:a.out.write_text(text,encoding='utf-8')
    print(text)
