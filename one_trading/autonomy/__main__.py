"""python -m one_trading.autonomy --help"""
import argparse,json
from pathlib import Path
from .config import DEFAULT
from .store import Store,canonical,atomic_bytes
from .engine import tick,status

def main():
    p=argparse.ArgumentParser(description="Separate hourly public-data / paper-only dual engine")
    p.add_argument("--runtime",required=True,help="Private directory outside any Git checkout")
    p.add_argument("--config",help="Immutable experiment JSON; default is built-in frozen version")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    t=sub.add_parser("tick");t.add_argument("--stop-after",choices=["packet","registration"],help="Durable restart verification; never changes wall clock")
    sub.add_parser("status")
    sub.add_parser("seal")
    sub.add_parser("export")
    g=sub.add_parser("register-growth");g.add_argument("spec")
    args=p.parse_args()
    config=json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else dict(DEFAULT)
    if args.command in ("init","tick","seal","register-growth"):
        from .validation import validate_repository
        from .store import validate_config
        validate_config(config);validate_repository(config)
    if args.command=="seal":
        from .validation import source_identity
        from .store import process_lock,digest
        with process_lock(args.runtime):
            s=Store(args.runtime)
            try:result=s.append("deployment","sealed",dict(source_hash=source_identity(),config_hash=digest(config)))
            finally:s.close()
    elif args.command=="init":
        s=Store(args.runtime)
        try:result=s.frozen_config(config)
        finally:s.close()
    elif args.command=="tick":result=tick(args.runtime,config,args.stop_after)
    elif args.command=="status":result=status(args.runtime,config)
    elif args.command=="register-growth":
        from .growth import register_growth
        from .store import process_lock
        with process_lock(args.runtime):
            s=Store(args.runtime)
            try:result=register_growth(s,config,json.loads(Path(args.spec).read_text(encoding="utf-8")))
            finally:s.close()
    else:
        s=Store(args.runtime)
        try:
            rows=[dict(r) for r in s.db.execute("SELECT * FROM records ORDER BY sequence")]
            for r in rows:r["body"]=json.loads(r["body"])
            atomic_bytes(s.root/"ledger-export.json",canonical(rows));result={"records":len(rows),"verified":s.verify()}
        finally:s.close()
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))

if __name__=="__main__":main()
