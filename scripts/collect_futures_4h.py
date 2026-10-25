"""Collect/audit public futures data, never account/order endpoints."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from one_trading.futures_data_audit import collect
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    p.add_argument('--workers',type=int,default=24);a=p.parse_args()
    if not 1<=a.workers<=64:p.error('workers 1..64')
    collect(a.output,a.workers)
