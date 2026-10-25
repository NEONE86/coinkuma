"""Audit official futures source bytes; no orders or strategy-return estimates."""
from __future__ import annotations
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
import csv
from datetime import datetime,timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import statistics
import zipfile

from .historical_data import Cache,BULK,ms,write_json
from .observer import json_text
from tools.probe_futures_sources import listing

HOUR=3_600_000
SYMBOLS=('BTCUSDT','ETHUSDT')
STREAMS=('klines','markPriceKlines','indexPriceKlines','fundingRate','metrics')


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


def parse_row(stream,symbol,row):
    if stream=='metrics':
        if len(row)<4 or row[1]!=symbol:raise ValueError('metric_symbol_or_shape')
        dt=datetime.fromisoformat(row[0]);dt=dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        t=int(dt.timestamp()*1000);values=[float(row[2]),float(row[3])]
        if t%300_000 or not all(math.isfinite(v) and v>=0 for v in values):raise ValueError('invalid_OI')
        return (t,*values)
    if stream=='fundingRate':
        t=int(row[0]);interval=float(row[1]);rate=float(row[2])
        if not math.isfinite(interval) or interval<=0 or not math.isfinite(rate) or abs(rate)>=1:raise ValueError('invalid_funding')
        return (t,interval,rate)
    if len(row)!=12:raise ValueError('kline_shape')
    t,end=int(row[0]),int(row[6]);values=[float(row[i]) for i in (1,2,3,4,5,7,10)]
    o,h,l,c,v,q,tq=values
    if t%HOUR or not t<=end<t+HOUR or not all(math.isfinite(x) for x in values):raise ValueError('kline_time_or_finite')
    if min(o,h,l,c)<=0 or min(v,q,tq)<0 or l>min(o,c) or h<max(o,c) or tq>q+1e-6*max(q,1):raise ValueError('kline_values')
    return (t,end,*values)


def inspect_zip(raw,stream,symbol,key,start,end):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if len(z.namelist())!=1 or not z.namelist()[0].endswith('.csv'):raise ValueError('zip_members')
        if z.getinfo(z.namelist()[0]).file_size>20_000_000:raise ValueError('csv_size')
        rows=list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode('utf-8-sig'))))
    c=Counter();errors=Counter();unique={};conflicts=set();previous=None
    expected=re.search(r'(\d{4}-\d{2}(?:-\d{2})?)\.zip$',key).group(1)
    known_header={'metrics':'create_time','fundingRate':'calc_time'}.get(stream,'open_time')
    if rows and rows[0] and rows[0][0]==known_header:c['header_rows']=1;rows=rows[1:]
    for row in rows:
        c['raw_rows']+=1
        try:
            v=parse_row(stream,symbol,row);t=v[0]
            stamp=datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%d' if stream=='metrics' else '%Y-%m')
            if stamp!=expected:raise ValueError('row_outside_filename_period')
            if not start<=t<end:continue
        except (ValueError,TypeError,IndexError,OverflowError) as e:
            c['invalid_rows']+=1;errors[str(e)]+=1;continue
        if previous is not None and t<previous:c['out_of_order_transitions']+=1
        previous=t
        if t in unique:
            c['duplicate_rows']+=1
            if unique[t]!=v:conflicts.add(t);c['conflicting_duplicate_rows']+=1
        else:unique[t]=v
        if stream=='fundingRate':
            if t%HOUR:c['funding_subhour_timestamps']+=1
        elif stream!='metrics' and v[1]!=t+HOUR-1:c['shortened_bars']+=1
    for t in conflicts:unique.pop(t,None)
    ordered=[unique[t] for t in sorted(unique)]
    c['conflicting_timestamps']=len(conflicts);c['usable_unique_rows']=len(ordered)
    if stream!='fundingRate':
        step=300_000 if stream=='metrics' else HOUR
        begin=ms(expected+('' if stream=='metrics' else '-01'))
        if stream=='metrics':stop=begin+24*HOUR
        else:
            year,month=map(int,expected.split('-'));stop=ms(f'{year+int(month==12):04d}-{month%12+1:02d}-01')
        c['expected_grid_rows']=(min(end,stop)-max(start,begin))//step
        c['missing_or_ambiguous_grid_rows']=c['expected_grid_rows']-len(ordered)
    return dict(counts=dict(c),errors=dict(errors),first_event_ms=ordered[0][0] if ordered else None,
                last_event_ms=ordered[-1][0] if ordered else None,normalized_sha256=digest(ordered),
                historical_availability_verified_rows=0,
                publication_field_present=False,publication_evidence='NOT_PROVIDED_BY_CSV')


def aggregate(files,index_evidence,plan):
    groups={}
    for f in files:
        key=f['symbol']+'__'+f['stream']+'__'+f['period'][:4]
        g=groups.setdefault(key,dict(symbol=f['symbol'],stream=f['stream'],year=f['period'][:4],files=0,counts=Counter(),
            first_event_ms=None,last_event_ms=None,object_modified_over_48h_after_last_event=0,object_lags_days=[]))
        g['files']+=1;g['counts'].update(f['audit']['counts'])
        a,z=f['audit']['first_event_ms'],f['audit']['last_event_ms']
        if a is not None:
            g['first_event_ms']=min(x for x in [g['first_event_ms'],a] if x is not None)
            g['last_event_ms']=max(x for x in [g['last_event_ms'],z] if x is not None)
            lag=(ms(f['last_modified'].replace('Z','+00:00'))-z)/(24*HOUR);g['object_lags_days'].append(lag)
            g['object_modified_over_48h_after_last_event']+=lag>2
    for g in groups.values():
        g['counts']=dict(g['counts']);lags=g.pop('object_lags_days')
        if g['stream']!='fundingRate':
            year=int(g['year']);lo=max(ms(plan['candidate_collection_interval'][0]),ms(f'{year}-01-01'))
            hi=min(ms(plan['candidate_collection_interval'][1]),ms(f'{year+1}-01-01'))
            step=300_000 if g['stream']=='metrics' else HOUR
            g['calendar_expected_rows']=(hi-lo)//step
            g['missing_file_grid_rows']=g['calendar_expected_rows']-g['counts'].get('expected_grid_rows',0)
            g['total_unavailable_grid_rows']=g['calendar_expected_rows']-g['counts']['usable_unique_rows']
        g['object_modification_lag_days_max']=max(lags) if lags else None
        g['object_modification_lag_days_median']=statistics.median(lags) if lags else None
    return dict(decision='NEEDS_MORE_EVIDENCE',stage='DATA_AVAILABILITY',strategy_backtest_executed=False,
        source_zip_count=len(files),index_page_count=len(index_evidence),symbols=list(SYMBOLS),
        collection_interval=plan['candidate_collection_interval'],evaluation_interval=plan['candidate_evaluation_interval'],
        source_manifest_sha256=digest(sorted((f['url'],f['sha256'],f['checksum_sha256'],f['last_modified']) for f in files)),
        normalized_manifest_sha256=digest(sorted((f['url'],f['audit']['normalized_sha256']) for f in files)),
        publisher_checksums_verified=len(files),historical_availability_verified_rows=0,
        groups=groups,blocking_reasons=['OI historical publication/availability timestamps are not supplied',
            'Archive LastModified is not an independently observed historical publication record',
            'No complete historical contract/filter/maintenance-margin/settlement-mark record'],
        performance_metrics=None,live_orders=False,leverage_applied=False)


def collect(output,workers=24):
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    plan=json.loads((Path(__file__).resolve().parents[1]/'docs/FUTURES_4H_AUDIT_PLAN.json').read_text(encoding='utf-8'))
    start,end=map(ms,plan['candidate_collection_interval']);cache=Cache(out/'http');tasks=[];evidence=[]
    for symbol in SYMBOLS:
        for stream in STREAMS:
            prefix=f'data/futures/um/{"daily" if stream=="metrics" else "monthly"}/{stream}/{symbol}/'
            if stream not in {'metrics','fundingRate'}:prefix+='1h/'
            entries,ev=listing(cache,prefix);evidence.extend(ev)
            for e in entries:
                match=re.search(r'(\d{4}-\d{2}(?:-\d{2})?)\.zip$',e['key'])
                if match:
                    period=match.group(1);t=ms(period+('-01' if len(period)==7 else ''))
                    if start<=t<end:tasks.append(dict(**e,symbol=symbol,stream=stream,period=period))
    tasks.sort(key=lambda x:x['key']);write_json(out/'collection_plan.json',dict(tasks=tasks,index_evidence=evidence,plan=plan))
    print(json.dumps(dict(phase='planned',zips=len(tasks))),flush=True)
    def fetch(t):
        raw,m=cache.get(BULK+t['key']);check,cm=cache.get(BULK+t['key']+'.CHECKSUM')
        tokens=check.decode().strip().split()
        if len(tokens)!=2 or tokens[0]!=hashlib.sha256(raw).hexdigest() or tokens[1].lstrip('*')!=t['key'].split('/')[-1]:raise ValueError('publisher_checksum')
        return dict(**t,url=m['url'],sha256=m['sha256'],checksum_sha256=cm['sha256'],fetched_ms=m['fetched_ms'],
                    audit=inspect_zip(raw,t['stream'],t['symbol'],t['key'],start,end))
    files=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0,len(tasks),workers*4):
            for f in as_completed([pool.submit(fetch,t) for t in tasks[offset:offset+workers*4]]):files.append(f.result())
            write_json(out/'progress.json',dict(done=len(files),total=len(tasks)))
            print(json.dumps(dict(phase='audited',done=len(files),total=len(tasks))),flush=True)
    files.sort(key=lambda x:x['key']);write_json(out/'file_audits.json',files)
    report=aggregate(files,evidence,plan);write_json(out/'data_audit.json',report)
    print(json.dumps(dict(phase='complete',decision=report['decision'],zips=len(files),source_sha=report['source_manifest_sha256'])),flush=True)
