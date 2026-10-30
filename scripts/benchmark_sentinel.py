"""Offline accepted-path 1h/24h workload. SYNTHETIC, accelerated, never a live soak.

The production byte cap is untouched. A separately named synthetic fixture policy
allows the requested cumulative workload to be processed and measured locally.
"""
import argparse
import hashlib
import json
import socket
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from one_sentinel.engine import Observer
from one_sentinel.store import raw_usage
from one_sentinel.demo import T


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=86400);a=p.parse_args()
    if a.out.exists():raise ValueError('FRESH_FIXTURE_DIRECTORY_REQUIRED')
    a.out.mkdir(parents=True)
    def no_network(*args,**kwargs):raise AssertionError('OFFLINE_FIXTURE_ONLY')
    socket.socket.connect=no_network;socket.create_connection=no_network
    base=Path(__file__).resolve().parents[1]
    policy=json.loads((base/'configs/sentinel_policy_v1.json').read_text())
    original_cap=policy['proposed_new_trial_limits']['raw_bytes_per_day']
    policy['proposed_new_trial_limits']['raw_bytes_per_day']=8*1024**3
    policy['fixture_only']='SYNTHETIC_ACCELERATED_RESOURCE_BENCHMARK_NO_NETWORK'
    path=a.out/'SYNTHETIC_ONLY_policy.json';path.write_text(json.dumps(policy,indent=2),encoding='utf-8')
    o=Observer(a.out/'runtime',path)
    symbols=policy['deep_initial']+[f'FIXTURE{i:04}USDT' for i in range(770)]
    meta={'symbols':[dict(symbol=s,quoteAsset='USDT',status='TRADING',isSpotTradingAllowed=True,
         filters=[dict(filterType='LOT_SIZE',stepSize='0.001',minQty='0.001'),dict(filterType='NOTIONAL',minNotional='10')]) for s in symbols]}
    def padded(value,size):
        raw=json.dumps(value,separators=(',',':')).encode()
        return raw+b' '*max(0,size-len(raw))
    metadata=padded(meta,6783086)
    samples=[];observations=[];service_total=0;backlog=0;max_backlog=0;known=0;counts={};chain=hashlib.sha256();began=time.monotonic();milestones=[]
    def put(source,raw,at):
        nonlocal service_total,known,backlog,max_backlog
        start=time.perf_counter();r=o.ingest(source,raw,at,at,'SYNTHETIC');elapsed=time.perf_counter()-start
        assert o.l.get('input',r['id']) is not None,'ACCEPTED_PATH_REQUIRED'
        samples.append(elapsed);service_total+=elapsed;known+=len(raw)
        counts[source]=counts.get(source,0)+1;chain.update(r['id'].encode())
        observations.append(dict(source=source,at=at,bytes=len(raw),receipt_id=r['id'],service_seconds=elapsed))
        backlog+=elapsed;max_backlog=max(max_backlog,backlog)
    for second in range(a.seconds):
        at=T+second
        backlog=max(0,backlog-1)
        if second%300==0:put('metadata',metadata,at)
        values=[dict(e='24hrMiniTicker',E=int(at*1000),s=s,c='100',v='1000',q='100000') for s in symbols[:120]]
        put('mini',padded(dict(stream='!miniTicker@arr',data=values),20774),at)
        if second%60==59:
            for symbol in symbols[:5]:
                end=int(at//60*60*1000)-1
                value=dict(stream=symbol.lower()+'@kline_1m',data=dict(s=symbol,E=int(at*1000),k=dict(i='1m',x=True,t=end-59999,T=end,q='20000')))
                put('bar',padded(value,360),at)
        if second+1 in (3600,a.seconds) or (second+1)%3600==0:
            checkpoint=dict(simulated_seconds=second+1,wall_elapsed_seconds=time.monotonic()-began,
                counts=dict(counts),ledger_rows=o.l.db.execute('select count(*) from records').fetchone()[0],
                cumulative_received_bytes=known,charged_rolling_24h_bytes=raw_usage(o.l,at),
                mean_ingest_ms=statistics.mean(samples)*1000,p95_ingest_ms=sorted(samples)[int(.95*(len(samples)-1))]*1000,
                max_ingest_ms=max(samples)*1000,serial_service_seconds=service_total,
                simulated_backlog_seconds=backlog,max_simulated_backlog_seconds=max_backlog,
                receipt_id_digest=chain.hexdigest(),classification='SYNTHETIC_ACCELERATED_NOT_1H_OR_24H_LIVE_OPERATION')
            milestones.append(checkpoint);print(json.dumps(checkpoint),flush=True)
            (a.out/'progress.json').write_text(json.dumps(milestones,indent=2),encoding='utf-8')
    status=o.status();contract=o.l.get('contract','v1');o.close()
    result=dict(milestones=milestones,status=status,source_hash=contract['source_hash'],policy_hash=contract['policy_hash'],script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),production_cap_unchanged=original_cap,
                fixture_cap=8*1024**3,fixture_mini_items_per_frame=120,metadata_symbols=775,
                network_calls=0,investment_edge_validated=False,
                raw_physical_bytes=sum(p.stat().st_size for p in (a.out/'runtime/raw').glob('*.bin')),
                sqlite_bytes=(a.out/'runtime/observer.sqlite3').stat().st_size,
                limits='Accelerated single-process accepted synthetic inputs; constant synthetic prices, repeated metadata bytes deduplicate physically. No TLS/WS buffering, real gaps, model, economic event frequency, or live 24h coverage measured.')
    (a.out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
    (a.out/'service_samples.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in observations),encoding='utf-8')


if __name__=='__main__':main()
