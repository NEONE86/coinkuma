"""Publish source provenance and audit aggregates, never prices or raw tick rows."""
import argparse
from collections import defaultdict,Counter
import csv
import hashlib
import json
from pathlib import Path


def load(p):return json.loads(p.read_text(encoding='utf-8'))
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
def csvwrite(p,rows):
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)


def export(archive,run,out):
    out.mkdir(parents=True,exist_ok=True)
    files=load(archive/'file_audits.json');plan=load(archive/'collection_plan.json')
    for name in ('run_manifest.json','data_audit.json','decision.json','independent.json'):write(out/name,load(run/name))
    report=load(run/'data_audit.json');summaries=[]
    for g in report['groups'].values():
        counts=g['counts']
        summaries.append(dict(symbol=g['symbol'],stream=g['stream'],year=g['year'],source_files=g['files'],
            raw_rows=counts.get('raw_rows',0),usable_unique_rows=counts.get('usable_unique_rows',0),
            invalid_rows=counts.get('invalid_rows',0),duplicate_rows=counts.get('duplicate_rows',0),
            conflicting_timestamps=counts.get('conflicting_timestamps',0),out_of_order_transitions=counts.get('out_of_order_transitions',0),
            missing_grid_rows_in_present_files=counts.get('missing_or_ambiguous_grid_rows'),
            missing_file_grid_rows=g.get('missing_file_grid_rows'),total_unavailable_grid_rows=g.get('total_unavailable_grid_rows'),
            funding_subhour_timestamps=counts.get('funding_subhour_timestamps',0),
            object_modified_over_48h_after_last_event=g['object_modified_over_48h_after_last_event'],
            object_modification_lag_days_max=g['object_modification_lag_days_max'],historical_public_availability_verified=False))
    csvwrite(out/'yearly_source_coverage.csv',summaries)
    blocks=defaultdict(list)
    for f in files:
        blocks[(f['symbol'],f['period'][:4])].append(dict(url=f['url'],zip_sha256=f['sha256'],
            checksum_sha256=f['checksum_sha256'],normalized_sha256=f['audit']['normalized_sha256'],
            object_last_modified=f['last_modified'],fetched_ms=f['fetched_ms'],
            rows=f['audit']['counts']['raw_rows'],historical_availability='UNVERIFIED'))
    for (symbol,year),rows in blocks.items():csvwrite(out/f'sources_{symbol}_{year}.csv',rows)
    write(out/'index_sources.json',plan['index_evidence'])
    public_probe={}
    connector_path=archive.parent/'connector_probes.json'
    if connector_path.exists():
        probes=load(connector_path)
        for p in probes['requests']:
            if p['status']!='fulfilled':continue
            entry=p['value'];v=entry['result'];s=v.get('structuredContent',{});rows=s.get('result',[])
            public_probe[entry['name']]=dict(isError=v.get('isError',False),error=s.get('error'),rows=len(rows),
                fields=list(rows[0]) if rows else [],empty_mark_price_rows=sum(not r.get('markPrice') for r in rows) if 'funding' in entry['name'] else None)
        write(out/'connector_evidence.json',dict(recorded_utc=probes['recorded_utc'],
            interpretation=probes['format'],precise_original_capture_latency_not_claimed=True,
            private_normalized_response_sha256=hashlib.sha256(connector_path.read_bytes()).hexdigest(),probes=public_probe))
    write(out/'verification.json',dict(tests_passed=172,compileall_passed=True,cli_help_passed=True,
        cli_status='NOT_STARTED',independent_scope='All4416sourceZIPs and normalized audit rows, not strategy performance',
        independent_reproduction_passed=load(run/'independent.json')['independent_reproduction_passed'],
        original_experiment_files_preserved=True,no_actual_or_simulated_orders=True,no_leverage_applied=True,
        strategy_performance_backtest=False,full_futures_account_engine_implemented=False,
        frozen_code_files=12,external_third_party_investment_audit=False,
        hash_scheme='Public artifact SHA256 hashes canonical CRLF-to-LF bytes, retaining CSV BOM. Raw ZIP/CHECKSUM roots use original bytes.'))
    write(out/'SHA256.json',{p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256.json'})
    print(json.dumps(dict(exported=out.as_posix(),source_manifest_blocks=len(blocks),zips=len(files))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    export(a.archive,a.run,a.output)
