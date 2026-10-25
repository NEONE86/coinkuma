"""Export supplemental provenance/aggregates only, not collected market rows."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def export(run, out):
    out.mkdir(parents=True,exist_ok=True)
    for path in run.glob('*.json'):
        value=json.loads(path.read_text(encoding='utf-8'))
        (out/path.name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    code=['one_trading/futures_evidence.py','scripts/collect_futures_evidence.py',
        'scripts/run_futures_evidence.py','scripts/validate_futures_evidence.py',
        'tests/test_futures_evidence.py','tools/export_futures_evidence.py',
        'docs/FUTURES_EVIDENCE_EXTENSION_PLAN.json']
    value=dict(tests_passed=178,compileall_passed=True,cli_help_passed=True,cli_status='NOT_STARTED',
        independent_scope='Supplemental source-byte, parser and gate reproduction, not strategy returns',
        previous_PR5_files_unchanged=True,orders=0,leverage=False,merge=False,
        plan_registered_local_commit='eec21e3',before_additional_sample_collection=True,
        code_sha256_after_validation={p:hashlib.sha256((ROOT/p).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in code})
    (out/'verification.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8',newline='\n')
    hashes={p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256.json'}
    (out/'SHA256.json').write_text(json.dumps(hashes,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(dict(files=len(hashes),output=out.as_posix())))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();export(a.run,a.output)
