import argparse,json
from pathlib import Path
from .worker import initialize,tick
from .audit import audit_runtime

def main():
    p=argparse.ArgumentParser(description='ONE frozen Alpha Loop public-data paper worker')
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--authority',type=Path,help='Existing authoritative runtime for a new prospective code cohort; never resets alpha')
    p.add_argument('command',choices=['init','tick','audit'])
    a=p.parse_args()
    if a.authority and a.command!='init':p.error('--authority is only for init')
    result=initialize(a.runtime,a.authority) if a.command=='init' else {'tick':tick,'audit':audit_runtime}[a.command](a.runtime)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
