"""Repeatable offline CI: existing regressions, fresh ledgers, independent audits.

Every generated record is synthetic. No market adapter or private evidence input.
Only summary.json is intended for the public Actions artifact.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def main(out):
    out=out.resolve();out.mkdir(parents=True,exist_ok=False)
    summary=dict(classification='SYNTHETIC_OFFLINE_NOT_MARKET',status='RUNNING',steps=[],
                 independent_cases=0,tamper_controls=0,actual_market_inputs=0,investment_edge_validated=False)
    env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
    for key in ('ONE_OBSERVER_WEBHOOK_URL','OPENAI_API_KEY'):env.pop(key,None)
    def save(): (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    def run(name,script,*args):
        result=subprocess.run([sys.executable,'-B','-X','utf8',str(ROOT/'scripts'/script),*map(str,args)],
                              cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=600)
        (out/(name+'.stdout.txt')).write_text(result.stdout,encoding='utf-8')
        (out/(name+'.stderr.txt')).write_text(result.stderr,encoding='utf-8')
        summary['steps'].append(dict(name=name,returncode=result.returncode));save()
        if result.returncode:raise RuntimeError('AUDIT_STEP_FAILED:'+name+'\n'+result.stdout+result.stderr)
        return json.loads(result.stdout)
    try:
        summary['unit_tests']=run('regressions','run_sentinel_offline_tests.py','--log',out/'tests.log')
        pending=out/'pending';bars=out/'bars'
        run('pending_epoch','replay_pending_epoch.py','--out',pending)
        run('bar_order','replay_bar_order.py','--out',bars)
        # The pending generator independently verifies all eleven exports itself.
        pending_results=json.loads((pending/'results.json').read_text(encoding='utf-8'))
        if len(pending_results)!=11:raise AssertionError('MISSING_PENDING_CASES')
        audits={}
        from verify_bar_order import projection
        for name in ('control','delayed','ordinary','other_symbol'):
            export=bars/name/'export'
            audits[name]=run('verify_'+name,'verify_sentinel_corrections.py',export)['status']
            projection(export)
            result=json.loads((bars/name/'result.json').read_text(encoding='utf-8'))
            if result['action']!='ENTER' or result['reason']!='ACCESS_HYPOTHESIS_ELIGIBLE':raise AssertionError('BAR_DECISION_CHANGED')
            if name=='delayed' and result['pending_outcomes'][0]['reason']!='OUT_OF_ORDER_BAR_TIME':raise AssertionError('OLD_BAR_NOT_REJECTED')
        summary['independent_cases']=len(pending_results)+len(audits)
        a=run('pending_tampers','tamper_pending_epoch.py','--evidence',pending,'--out',out/'pending_tampers')
        b=run('bar_tampers','tamper_bar_order.py','--after',bars,'--out',out/'bar_tampers')
        if len(a)!=3 or len(b)!=2:raise AssertionError('MISSING_NEGATIVE_CONTROLS')
        summary['tamper_controls']=len(a)+len(b)
        summary['tamper_reasons']={**{k:v['reason'] for k,v in a.items()},**{k:v['reason'] for k,v in b.items()}}
        summary['status']='PASS_OFFLINE_ONLY';save();print(json.dumps(summary,indent=2))
    except BaseException:
        summary['status']='FAILED';save();raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);a=p.parse_args();main(a.out)
