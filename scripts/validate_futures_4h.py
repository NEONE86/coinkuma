"""Independent raw-byte/row reproduction, not a profitability audit."""
from collections import Counter,defaultdict
import csv
from datetime import datetime,timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

HOUR=3_600_000
NS={'s':'http://s3.amazonaws.com/doc/2006-03-01/'}


def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def utc(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()*1000)


def cache_body(cache,url):
    stem=cache/sha(url.encode());meta=json.loads(stem.with_suffix('.json').read_text(encoding='utf-8'))
    raw=stem.with_suffix('.bin').read_bytes()
    if meta['url']!=url or sha(raw)!=meta['sha256']:raise AssertionError('cache_raw_integrity')
    return raw,meta


def reconstruct(raw,stream,symbol,key,start,end):
    """Separate parser/grouping implementation; imports no strategy parser."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=z.namelist()
        if len(names)!=1 or not names[0].endswith('.csv'):raise AssertionError('zip_shape')
        all_rows=list(csv.reader(io.StringIO(z.read(names[0]).decode('utf-8-sig'))))
    header_name={'metrics':'create_time','fundingRate':'calc_time'}.get(stream,'open_time')
    header=[];counts=Counter()
    if all_rows and all_rows[0] and all_rows[0][0]==header_name:
        header=all_rows.pop(0);counts['header_rows']=1
    period=re.search(r'(\d{4}-\d{2}(?:-\d{2})?)\.zip$',key).group(1)
    by_time=defaultdict(list);original_times=[];all_values=[]
    for row in all_rows:
        counts['raw_rows']+=1
        try:
            if stream=='metrics':
                if len(row)<4 or row[1]!=symbol:raise ValueError()
                t=utc(row[0]);v=(t,float(row[2]),float(row[3]))
                if t%300_000 or any(not math.isfinite(x) or x<0 for x in v[1:]):raise ValueError()
            elif stream=='fundingRate':
                v=(int(row[0]),float(row[1]),float(row[2]));t=v[0]
                if not math.isfinite(v[1]) or v[1]<=0 or not math.isfinite(v[2]) or abs(v[2])>=1:raise ValueError()
            else:
                if len(row)!=12:raise ValueError()
                t=int(row[0]);last=int(row[6]);o,h,l,c,vol,q,tq=[float(row[i]) for i in (1,2,3,4,5,7,10)]
                if t%HOUR or not t<=last<t+HOUR or not all(math.isfinite(x) for x in (o,h,l,c,vol,q,tq)):raise ValueError()
                if min(o,h,l,c)<=0 or min(vol,q,tq)<0 or min(o,c)<l or max(o,c)>h or tq>q+1e-6*max(q,1):raise ValueError()
                v=(t,last,o,h,l,c,vol,q,tq)
            stamp=datetime.fromtimestamp(t/1000,timezone.utc).isoformat()[:10 if stream=='metrics' else 7]
            if stamp!=period:raise ValueError()
            if not start<=t<end:continue
            by_time[t].append(v);original_times.append(t);all_values.append(v)
        except (ValueError,TypeError,IndexError,OverflowError):counts['invalid_rows']+=1
    counts['duplicate_rows']=sum(len(v)-1 for v in by_time.values())
    conflicts={t for t,v in by_time.items() if len(set(v))>1}
    counts['conflicting_timestamps']=len(conflicts)
    counts['conflicting_duplicate_rows']=sum(sum(v!=vs[0] for v in vs[1:]) for vs in by_time.values())
    counts['out_of_order_transitions']=sum(b<a for a,b in zip(original_times,original_times[1:]))
    valid=[values[0] for t,values in sorted(by_time.items()) if t not in conflicts]
    counts['usable_unique_rows']=len(valid)
    if stream=='fundingRate':counts['funding_subhour_timestamps']=sum(v[0]%HOUR!=0 for v in all_values)
    elif stream!='metrics':counts['shortened_bars']=sum(v[1]!=v[0]+HOUR-1 for v in all_values)
    if stream!='fundingRate':
        begin=utc(period+('-01' if len(period)==7 else ''))
        if stream=='metrics':stop=begin+24*HOUR;step=300_000
        else:
            year,month=map(int,period.split('-'));stop=utc(f'{year+int(month==12):04d}-{month%12+1:02d}-01');step=HOUR
        counts['expected_grid_rows']=(min(end,stop)-max(start,begin))//step
        counts['missing_or_ambiguous_grid_rows']=counts['expected_grid_rows']-len(valid)
    publication_columns=[h for h in header if re.search('publish|availab',h,re.I)]
    return counts,sha(canonical(valid)),{v[0] for v in valid},publication_columns


def verify(archive,run_dir,output):
    cache=archive/'http';plan=json.loads((archive/'collection_plan.json').read_text(encoding='utf-8'))
    files=json.loads((archive/'file_audits.json').read_text(encoding='utf-8'))
    report=json.loads((run_dir/'data_audit.json').read_text(encoding='utf-8'))
    decision=json.loads((run_dir/'decision.json').read_text(encoding='utf-8'))
    start,end=map(utc,plan['plan']['candidate_collection_interval'])
    indices={}
    for ev in plan['index_evidence']:
        raw,meta=cache_body(cache,ev['url'])
        if meta['sha256']!=ev['sha256']:raise AssertionError('index_evidence_changed')
        root=ET.fromstring(raw)
        for e in root.findall('s:Contents',NS):
            indices[e.findtext('s:Key',namespaces=NS)]=(e.findtext('s:LastModified',namespaces=NS),int(e.findtext('s:Size',namespaces=NS)))
    if len({f['url'] for f in files})!=len(files) or len(files)!=len(plan['tasks']):raise AssertionError('source_inventory')
    normalized=[];sources=[];totals=Counter();grids=defaultdict(set);publication_fields=set()
    for i,f in enumerate(files):
        raw,m=cache_body(cache,f['url']);check,cm=cache_body(cache,f['url']+'.CHECKSUM')
        tokens=check.decode().split()
        if len(tokens)!=2 or tokens[0]!=sha(raw) or tokens[1].lstrip('*')!=f['key'].split('/')[-1]:raise AssertionError('publisher_checksum')
        if sha(raw)!=f['sha256'] or cm['sha256']!=f['checksum_sha256']:raise AssertionError('captured_source_hash')
        if indices[f['key']]!=(f['last_modified'],len(raw)):raise AssertionError('object_metadata')
        counts,normalized_sha,times,columns=reconstruct(raw,f['stream'],f['symbol'],f['key'],start,end)
        if counts!=Counter(f['audit']['counts']) or normalized_sha!=f['audit']['normalized_sha256']:raise AssertionError('derived_row_audit:'+f['key'])
        totals.update(counts);grids[(f['symbol'],f['stream'])].update(times);publication_fields.update(columns)
        normalized.append((f['url'],normalized_sha));sources.append((f['url'],sha(raw),cm['sha256'],f['last_modified']))
        if (i+1)%500==0:print(json.dumps(dict(phase='raw_reproduction',files=i+1)),flush=True)
    source_root=sha(canonical(sorted(sources)));normalized_root=sha(canonical(sorted(normalized)))
    if source_root!=report['source_manifest_sha256'] or normalized_root!=report['normalized_manifest_sha256']:raise AssertionError('manifest_root')
    a,z=map(utc,report['evaluation_interval']);cut=utc('2025-01-01');four_hour={};prefix={}
    for symbol in report['symbols']:
        hours=grids[(symbol,'klines')]
        points=[t for t in range(a,z,4*HOUR) if all(t+j*HOUR in hours for j in range(4))]
        four_hour[symbol]=len(points)
        deleted_future={t for t in hours if t<cut}
        prefix[symbol]=sum(all(t+j*HOUR in deleted_future for j in range(4)) for t in range(a,cut,4*HOUR))
        if prefix[symbol]!=sum(t<cut for t in points):raise AssertionError('data_grid_future_prefix')
    if publication_fields:raise AssertionError('new_publication_columns_require_semantic_review')
    if report['historical_availability_verified_rows']!=0 or report['verified_signal_observations']!=0:raise AssertionError('invented_PIT_evidence')
    if decision['strategy_backtest_executed'] or any(v is not None for v in decision['performance_metrics'].values()):raise AssertionError('invented_performance')
    if decision['decision']!='NEEDS_MORE_EVIDENCE' or decision['data_gate_passed']:raise AssertionError('unjustified_approval')
    result=dict(independent_reproduction_passed=True,scope='SOURCE_BYTES_ROWS_AND_DATA_GATE_ONLY_NOT_STRATEGY_RETURNS',
        files_verified=len(files),index_pages_verified=len(plan['index_evidence']),counts=dict(totals),
        source_manifest_sha256=source_root,normalized_manifest_sha256=normalized_root,
        complete_4h_trade_observations=four_hour,pre2025_grid_counts_after_future_deletion=prefix,
        historical_OI_availability_verified=0,publication_columns_found=[],performance_backtest_not_executed=True,
        main_gate_reproduced='NEEDS_MORE_EVIDENCE',external_investment_audit=False)
    if output.exists():raise ValueError('independent_output_exists')
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    verify(a.archive,a.run,a.output)
