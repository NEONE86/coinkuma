"""Freeze a single research policy; refuse unqualified futures performance runs."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.futures_data_audit import aggregate,HOUR
from one_trading.futures_4h_research import data_gate
from one_trading.historical_data import ms,write_json

FROZEN_PATHS=['docs/FUTURES_4H_AUDIT_PLAN.json','docs/FUTURES_4H_PROTOCOL.json',
    'one_trading/__init__.py','one_trading/historical_data.py','one_trading/observer.py',
    'one_trading/futures_data_audit.py','one_trading/futures_4h_research.py',
    'scripts/collect_futures_4h.py','scripts/run_futures_4h.py','scripts/validate_futures_4h.py',
    'tools/probe_futures_sources.py','tests/test_futures_research.py']
PARENT='e49f3f06518eb3bf4ea59e82680f96c46acf2828'


def hashes():return {p:hashlib.sha256((ROOT/p).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in FROZEN_PATHS}
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))


def freeze(path):
    if path.exists():raise ValueError('existing_freeze_preserved')
    value=dict(frozen_utc=datetime.now(timezone.utc).isoformat(),parent_pr4_head=PARENT,
        code_sha256=hashes(),protocol=load(ROOT/'docs/FUTURES_4H_PROTOCOL.json'),
        before_strategy_performance=True,data_availability_audit_precedes_rule_freeze=True,
        performance_known=False,source='Official archives only, data qualification before any strategy PnL')
    write_json(path,value);print(json.dumps(dict(phase='FROZEN_NO_PERFORMANCE',files=len(FROZEN_PATHS))),flush=True)


def run(path,archive,out):
    frozen=load(path)
    if frozen['code_sha256']!=hashes():raise ValueError('frozen_code_changed_use_new_experiment')
    rel=path.resolve().relative_to(ROOT).as_posix()
    if json.loads(subprocess.check_output(['git','show','HEAD:'+rel],cwd=ROOT))!=frozen:raise ValueError('freeze_must_be_committed')
    subprocess.run(['git','merge-base','--is-ancestor',PARENT,'HEAD'],cwd=ROOT,check=True)
    out.mkdir(parents=True,exist_ok=False)
    plan=load(archive/'collection_plan.json');files=load(archive/'file_audits.json')
    report=aggregate(files,plan['index_evidence'],plan['plan'])
    a,z=map(ms,frozen['protocol']['evaluation'])
    report.update(required_signal_observations=len(frozen['protocol']['scope'])*(z-a)//(4*HOUR),
        verified_signal_observations=0,historical_contract_terms_verified=False,exact_funding_settlement_marks_verified=False)
    decision=data_gate(report)
    write_json(out/'run_manifest.json',dict(frozen_code=hashes(),freeze_sha256_canonical_lf=hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        started_utc=datetime.now(timezone.utc).isoformat(),historical_performance_computed=False,
        archive_file_audits_sha256=hashlib.sha256((archive/'file_audits.json').read_bytes()).hexdigest()))
    write_json(out/'data_audit.json',report);write_json(out/'decision.json',decision)
    print(json.dumps(dict(phase='DATA_GATE_COMPLETED',decision=decision)),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--freeze',type=Path,required=True)
    p.add_argument('--freeze-only',action='store_true');p.add_argument('--archive',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.freeze_only:freeze(a.freeze)
    else:
        if not a.archive or not a.output:p.error('--archive and --output required')
        run(a.freeze,a.archive,a.output)
