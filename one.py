#!/usr/bin/env python3
"""ONE Trading entry point. Python 3.10+, standard library only."""
from __future__ import annotations
import argparse,json,sys,webbrowser
from pathlib import Path
from one_trading.portfolio import validate_config
from one_trading.replay import run_replay
from one_trading.runtime import run
ROOT=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(description="ONE Trading v0.3.1 — experimental paper execution")
    parser.add_argument("command",choices=["replay","paper","run","status"])
    parser.add_argument("--config",type=Path,default=ROOT/"config.json")
    parser.add_argument("--workspace",type=Path,default=ROOT/"workspace")
    parser.add_argument("--output",type=Path,default=ROOT/"output/replay")
    parser.add_argument("--archive",type=Path,default=ROOT/"archive/observer_v02.sqlite3")
    parser.add_argument("--open",action="store_true",help="open local replay HTML")
    parser.add_argument("--cycles",type=int,default=0)
    args=parser.parse_args()
    c=json.loads(args.config.read_text(encoding="utf-8"));validate_config(c)
    if args.command=="replay":
        result=run_replay(c,args.archive,args.output)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.open:webbrowser.open((args.output/"dashboard.html").resolve().as_uri())
        return 0
    if args.command=="status":
        path=args.workspace/"status.json"
        print(path.read_text(encoding="utf-8") if path.exists() else '{"status":"NOT_STARTED"}')
        return 0
    return run(c,args.workspace,1 if args.command=="paper" else args.cycles)

if __name__=="__main__":
    try:raise SystemExit(main())
    except KeyboardInterrupt:
        print("STOPPED: process ended; no background server was started.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}",file=sys.stderr)
        raise SystemExit(2)
