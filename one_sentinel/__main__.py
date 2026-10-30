"""Offline by default; bounded public smoke requires an exact prior approval file."""
import argparse,asyncio,json,os
from pathlib import Path
from .engine import Observer
from .notify import dispatch_synthetic, analyze_one

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('action',choices=['replay','status','export','smoke']);p.add_argument('--input',type=Path);p.add_argument('--out',type=Path)
    p.add_argument('--approval',type=Path);p.add_argument('--seconds',type=int,default=60);a=p.parse_args()
    o=Observer(a.runtime)
    try:
        if a.action=='replay':
            for line in a.input.read_text(encoding='utf-8').splitlines():
                r=json.loads(line)
                if Path(r['raw_file']).is_absolute() or not (a.input.parent/r['raw_file']).resolve().is_relative_to(a.input.parent.resolve()):raise ValueError('INPUT_PATH_ESCAPE')
                raw=(a.input.parent/r['raw_file']).read_bytes()
                receipt=o.ingest(r['source'],raw,r['received_at'],r['processed_at'],r['classification'],r.get('origin'))
                if receipt.get('duplicate'):continue
                analyze_one(o.l,o.policy,r['processed_at'])
                if r['classification']=='SYNTHETIC':dispatch_synthetic(o.l,r['processed_at']+1)
        elif a.action=='export':o.export(a.out)
        elif a.action=='smoke':
            if not a.approval or not 1<=a.seconds<=172800:raise ValueError('BOUNDED_APPROVAL_REQUIRED')
            from .transport import smoke
            asyncio.run(smoke(o,json.loads(a.approval.read_text()),a.seconds,os.environ.get('ONE_OBSERVER_WEBHOOK_URL')))
        print(json.dumps(o.status(),indent=2))
    finally:o.close()

if __name__=='__main__':main()
