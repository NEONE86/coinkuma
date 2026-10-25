"""Offline, isolated evidence/panel/linkage reproduction without editing originals."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.value_evidence import save_json


def reproduce(evidence,original,atlas,destination):
    destination.mkdir(parents=True,exist_ok=False)
    copied=destination/'evidence'
    shutil.copytree(evidence,copied)
    out=destination/'study'
    commands=[
        ['scripts/collect_value_events.py','--output',str(copied),'--offline'],
        ['scripts/resolve_value_events.py','--evidence',str(copied),'--offline'],
        ['scripts/resolve_value_events.py','--evidence',str(copied),'--classifications',str(ROOT/'docs/VALUE_ACCRUAL_CLASSIFICATIONS.json'),'--offline'],
        ['scripts/run_value_event_study.py','--evidence',str(copied),'--atlas',str(atlas),'--freeze',str(ROOT/'docs/VALUE_ACCRUAL_FREEZE.json'),'--output',str(out)]]
    for i,command in enumerate(commands):
        with (destination/f'step-{i+1}.log').open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,'-X','utf8',*command],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    names=['summary.json','statistics.json','scheduled_events.json','collector_schedule.json',
           'matched_pairs_private.json','execution_context_private.json','source_cohorts_private.json']
    checks={name:(original/name).read_bytes()==(out/name).read_bytes() for name in names}
    checks['event_panel.json']=(evidence/'event_panel.json').read_bytes()==(copied/'event_panel.json').read_bytes()
    if not all(checks.values()):raise AssertionError('reproduction_changed_substantive_results')
    report=dict(status='PASS',network_calls=0,originals_modified=False,exact_byte_comparisons=checks,
        excluded_nondeterministic_files=['run_manifest.json','artifact_hashes.json'],
        explanation='Run time differs; all panel, clock, cohort, matching, endpoint and summary bytes match. Same-author isolated process, not external audit.')
    save_json(destination/'reproduction.json',report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('evidence','original','atlas','destination'):p.add_argument('--'+flag,type=Path,required=True)
    a=p.parse_args();reproduce(*(getattr(a,k).resolve() for k in ('evidence','original','atlas','destination')))
