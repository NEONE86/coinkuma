"""Freeze and link evidence to an existing immutable atlas; no account trading."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from one_trading.value_evidence import save_json,utc,sha,timestamp
from one_trading.value_event_study import schedule,clocks,select_controls,exposed_symbols,summarize
from one_trading.repricing_v3 import era_of,mature_in_era,END
from one_trading.observer import DAY

PARENT='35bfbbc2a71aa47579974d15e9d1c62e3ba7db55'
ATLAS_SHA='2fad9446a5b10955b16bf8106ea85e3fc57bd2c2da2a6fd9eb7c2eee04001bb7'
FILES=['docs/VALUE_ACCRUAL_PROTOCOL.json','docs/VALUE_ACCRUAL_CLASSIFICATIONS.json',
 'one_trading/value_evidence.py','one_trading/value_event_study.py',
 'scripts/collect_value_events.py','scripts/resolve_value_events.py',
 'scripts/run_value_event_study.py','scripts/validate_value_events.py','tests/test_value_events.py']


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def code_hashes():
    return {p:sha((ROOT/p).read_bytes().replace(b'\r\n',b'\n')) for p in FILES}


def freeze(path,evidence):
    if path.exists():raise ValueError('freeze_already_exists')
    save_json(path,dict(frozen_at=utc(),parent=PARENT,remote_protocol_commit='06d73b1a7e1e390ef54a75f596d4edb62cb6f3c9',
        code_sha256=code_hashes(),event_panel_sha256=file_hash(evidence/'event_panel.json'),
        atlas_sha256=ATLAS_SHA,new_event_price_outcomes_inspected=False,empirical_parameter_trials=0,
        earlier_atlas_results_already_known=True))


def validate_freeze(path,evidence):
    f=json.loads(path.read_text(encoding='utf-8'))
    if f['code_sha256']!=code_hashes():raise ValueError('event_code_changed_after_freeze')
    if file_hash(evidence/'event_panel.json')!=f['event_panel_sha256']:raise ValueError('event_panel_changed_after_freeze')
    relative=path.resolve().relative_to(ROOT).as_posix()
    if json.loads(subprocess.check_output(['git','show','HEAD:'+relative],cwd=ROOT))!=f:
        raise ValueError('freeze_not_committed')
    subprocess.run(['git','merge-base','--is-ancestor',PARENT,'HEAD'],cwd=ROOT,check=True)
    return f


def run(atlas,evidence,out,frozen):
    f=validate_freeze(frozen,evidence)
    if file_hash(atlas)!=ATLAS_SHA:raise ValueError('inherited_atlas_hash_mismatch')
    if out.exists():raise ValueError('output_already_exists')
    out.mkdir(parents=True)
    save_json(out/'run_manifest.json',dict(started_at=utc(),freeze=f,atlas_sha256=ATLAS_SHA,
        parent=PARENT,head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        study='descriptive_event_study',new_strategy=False,live_orders=False,leverage=False))
    events=json.loads((evidence/'event_panel.json').read_text(encoding='utf-8'))
    scheduled=schedule(events);strict=schedule(events,'collector')
    executions=[]
    for e in events:
        if e['direct_value_accrual'] and e.get('source_time_proven') and e.get('executed_at'):
            # Execution analysis is paired descriptive context, never added to
            # the primary observation count or a signal eligible at publication.
            d=(timestamp(e['executed_at'])+3600000)//DAY*DAY
            executions.append(dict(event_id=e['event_id'],symbol=e['symbol'],day=d,
                usable=bool(mature_in_era(d)),independent_observation=False))
    days={r['day'] for r in scheduled if not r['exclusion']} | {r['day'] for r in executions if r['usable']}
    cohorts=defaultdict(list);count=0
    with gzip.open(atlas,'rt',encoding='utf-8') as stream:
        for line in stream:
            r=json.loads(line);count+=1
            if r['day'] in days:cohorts[r['day']].append(r)
    if count!=473267:raise ValueError('unexpected_atlas_row_count')
    pairs=[]
    for r in scheduled:
        if r['exclusion']:continue
        cohort=cohorts[r['day']]
        cases=[c for c in cohort if c['symbol']==r['symbol']]
        if not cases:
            r['exclusion']='NO_ELIGIBLE_ATLAS_ROW';continue
        if len(cases)!=1 or cases[0]['boundary_purged']:raise ValueError('atlas_key_or_boundary')
        case=cases[0];excluded=exposed_symbols(events,r['day'])
        controls=select_controls(case,cohort,excluded)
        failures=select_controls(case,cohort,excluded,True)
        pairs.append(dict(event_id=r['event_id'],case=case,controls=controls,failure_only_controls=failures))
        r.update(control_symbols=[c['symbol'] for c in controls],failure_control_symbols=[c['symbol'] for c in failures],
                 controls_available=len(controls))
    for r in executions:
        r['atlas']=next((c for c in cohorts.get(r['day'],[]) if c['symbol']==r['symbol']),None) if r['usable'] else None
    stats=summarize(pairs)
    dates={p['case']['day'] for p in pairs}
    cases={(p['case']['symbol'],p['case']['day']) for p in pairs}
    bounded=[r for d in dates for r in cohorts[d]]
    success=sum(r['delayed']['180d_2x']['hit'] for r in bounded)
    failed=len(bounded)-success
    fp=sum(not p['case']['delayed']['180d_2x']['hit'] for p in pairs)
    riskset=dict(exact_event_dates=len(dates),eligible_symbol_dates=len(bounded),successes=success,failures=failed,
        base_success_fraction=success/len(bounded) if bounded else None,false_positive_event_signals=fp,
        bounded_date_signal_fpr=fp/failed if failed else None,
        warning='Signal means a selected primary event in this bounded panel, not absence/presence of all token events; these are selected event dates, not a market-wide FPR. Rows and tokens are dependent.')
    control_ids=[(c['symbol'],c['day']) for p in pairs for c in p['controls']]
    summary=dict(decision='NEEDS_MORE_EVIDENCE',validated_edge=False,live_eligible=False,
        inherited_atlas_rows_verified=count,panel_events=len(events),
        direct_events=sum(e['direct_value_accrual'] for e in events),
        primary_events=len(pairs),strict_collector_matured_events=sum(not r['exclusion'] for r in strict),
        exclusion_counts=dict(Counter(r['exclusion'] or 'JOINED' for r in scheduled)),
        collector_exclusion_counts=dict(Counter(r['exclusion'] or 'JOINED' for r in strict)),
        independent_tokens=len({p['case']['symbol'] for p in pairs}),
        halfyear_clusters=len({(datetime.fromtimestamp(p['case']['day']/1000,timezone.utc).year,
            (datetime.fromtimestamp(p['case']['day']/1000,timezone.utc).month-1)//6) for p in pairs}),
        control_reused_symbol_date_rows=len(control_ids)-len(set(control_ids)),
        reason='Selected three-token feasibility sample; no historical collector receipts; validation/challenge sample and cluster minima unmet; no new trading rule or profitability evidence.',
        bounded_riskset=riskset)
    save_json(out/'scheduled_events.json',scheduled);save_json(out/'collector_schedule.json',strict)
    save_json(out/'matched_pairs_private.json',pairs);save_json(out/'execution_context_private.json',executions)
    save_json(out/'statistics.json',stats);save_json(out/'summary.json',summary)
    save_json(out/'source_cohorts_private.json',{str(d):r for d,r in cohorts.items()})
    save_json(out/'artifact_hashes.json',{p.name:file_hash(p) for p in sorted(out.iterdir()) if p.is_file()})
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--atlas',type=Path);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--output',type=Path);p.add_argument('--freeze',type=Path,required=True);p.add_argument('--freeze-only',action='store_true')
    a=p.parse_args()
    if a.freeze_only:freeze(a.freeze,a.evidence)
    else:
        if not a.atlas or not a.output:p.error('--atlas and --output required')
        run(a.atlas,a.evidence,a.output,a.freeze)
