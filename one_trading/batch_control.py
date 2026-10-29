"""Owner-scoped claims and critical maintenance using the existing authority.

No budget/alpha reset and no reconciliation of disclosed overruns. Only already
registered case maintenance may reserve available time while research is halted.
"""
import json,math,time,uuid
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,process_lock
from .alpha_loop import worker as alpha
from .alpha_loop.reference.research_loop import KINDS,stamp
from datetime import timedelta

QUOTE_ATTEMPT_LIMIT=3
QUOTE_RETRY_SECONDS=120

def quote_state(s,c,case):
    attempts=[r for r in s.all('alpha_quote_attempt') if r['case_id']==case['id']]
    terminal=s.get('alpha_quote_terminal',case['id'],True)
    expiry=instant(case['cutoff'])+timedelta(seconds=c['decision_validity_seconds'])
    if case.get('due_at'):expiry=min(expiry,instant(case['due_at']))
    due=instant(attempts[-1]['at'])+timedelta(seconds=QUOTE_RETRY_SECONDS) if attempts else None
    return dict(attempts=len(attempts),limit=QUOTE_ATTEMPT_LIMIT,
        next_due=due.isoformat() if due else None,eligible=bool(due and instant(utc())>=due),
        expired=instant(utc())>expiry,deadline=expiry.isoformat(),terminal=bool(terminal))

def advance_with_quote_gate(s,c,case,task_key):
    """Operational retry only; sealed policies, model gates and fill rules stay intact.

    An attempted quote is durable before I/O, so a crash also consumes a slot.
    Missing matched diagnostics never acquire a zero-return CASH label.
    """
    complete=lambda: all(s.get('alpha_decision',case['id']+':'+a,True) for a in c['arms'])
    missing=lambda: not s.get('alpha_matched_context',case['id'],True)
    def terminal(reason):
        old=s.get('alpha_quote_terminal',case['id'],True)
        if not old:old=s.append('alpha_quote_terminal',case['id'],dict(id=case['id'],case_id=case['id'],at=utc(),reason=reason,status='MATCHED_QUOTE_MISSING',metrics=None))
        return dict(status=old['status'],case_id=case['id'],reason=old['reason'],metrics=None)
    def reserve():
        key=case['id']+':'+str(quote_state(s,c,case)['attempts']+1)
        return s.append('alpha_quote_attempt',key,dict(id=key,case_id=case['id'],at=utc(),task_key=task_key,
            limit=QUOTE_ATTEMPT_LIMIT,retry_seconds=QUOTE_RETRY_SECONDS))
    q=quote_state(s,c,case);attempt=None
    if complete() and missing():
        if q['terminal'] or q['expired'] or q['attempts']>=QUOTE_ATTEMPT_LIMIT:
            return terminal('DECISION_WINDOW_CLOSED' if q['expired'] else 'QUOTE_ATTEMPTS_EXHAUSTED')
        if q['attempts'] and not q['eligible']:return dict(status='WAITING_QUOTE_RETRY',case_id=case['id'],next_due=q['next_due'])
        attempt=reserve()
    result=alpha.advance(s,c,case)
    if complete() and missing():
        # First call can finish the decisions and encounter the quote error.
        # Its worker receipt closes that slot; subsequent retries reserve first.
        if not attempt:attempt=reserve()
        if quote_state(s,c,case)['attempts']>=QUOTE_ATTEMPT_LIMIT:return terminal('QUOTE_ATTEMPTS_EXHAUSTED')
    return result


def advance_action(s,c,case):
    """A bounded retry changes only when durable progress or a frozen clock gate
    changes. Minute-named keys with identical inputs violate authority identity.
    """
    rows=[];gates={}
    for arm in c['arms']:
        key=case['id']+':'+arm
        for kind,identity in [('alpha_decision',key)]+[('alpha_entry_result',key+':'+t) for t in ('NATIVE','MATCHED')]:
            row=s.get(kind,identity,True)
            if row:rows.append([kind,identity,digest(row)])
        calls=[]
        for attempt in range(c['max_model_attempts']):
            row=s.get('model_call',digest([key,'attempt',attempt+1]),True)
            if row:calls.append(row);rows.append(['model_call',row['id'],digest(row)])
        if calls and len(calls)<c['max_model_attempts']:
            due=instant(calls[-1]['completed_at'])+timedelta(seconds=c['model_retry_seconds'][len(calls)-1])
            gates[arm]=dict(due_at=due.isoformat(),eligible=instant(utc())>=due)
    return dict(operation='ADVANCE',case_id=case['id'],progress_hash=digest(rows),retry_gates=gates,quote_gate=quote_state(s,c,case),
        decision_window_expired=instant(utc())>instant(case['cutoff'])+timedelta(seconds=c['decision_validity_seconds']))


def claim_owned(loop,keys,critical=False,maintenance_bindings=None):
    keys=set(keys)
    with loop.transaction():
        candidates=[]
        for row in loop.db.execute("SELECT * FROM tasks WHERE state='QUEUED'").fetchall():
            if row['task_key'] not in keys:continue
            loop._expire(row['experiment_id']);experiment,spec=loop._experiment(row['experiment_id'])
            if experiment['state'] not in ('DATA_READY','FORWARD_TEST'):continue
            task=json.loads(row['task_json'])
            if task['due_at'] and instant(task['due_at'])>loop.now():continue
            engine=loop.db.execute('SELECT * FROM engine_budgets WHERE engine=?',(experiment['engine'],)).fetchone()
            # An existing paper position is operational work, not a forward
            # forecast label. Only the caller's durable position binding grants
            # this narrow halt exception; all original resource caps still apply.
            bound=(maintenance_bindings or {}).get(row['task_key'])
            position_maintenance=(bound==task['input_hash'] and bool(bound)
                and experiment['engine']=='CAPITAL_GROWTH'
                and spec['lineage'].startswith('existing-position-maintenance/')
                and task['kind']=='research')
            maintenance=task['cash_units']==0 and (critical and task['kind']=='due_label' or position_maintenance)
            if engine['engine_halted'] and not maintenance:continue
            if not loop._affordable(experiment,spec,engine,task):continue
            candidates.append((KINDS[task['kind']],task['due_at'] or '',task['task_key'],experiment,task))
        if not candidates:return None
        _,_,key,experiment,task=min(candidates,key=lambda x:x[:3]);token=uuid.uuid4().hex
        loop.db.execute("UPDATE tasks SET state='CLAIMED',claim_token=?,claimed_at=? WHERE task_key=?",(token,stamp(loop.now()),key))
        for table,column,value in (('experiments','id',experiment['id']),('engine_budgets','engine',experiment['engine'])):
            loop.db.execute(f'UPDATE {table} SET cash_reserved=cash_reserved+?,time_reserved=time_reserved+? WHERE {column}=?',(task['cash_units'],task['time_seconds'],value))
        loop._audit('TASK_CLAIMED',experiment['id'],task_key=key,claim_token=token,cash_reserved=task['cash_units'],time_reserved=task['time_seconds'],
                    owner_scope='EXISTING_POSITION' if position_maintenance else 'CRITICAL_EXISTING_CASE' if critical else 'EXPLICIT_OWNER_KEYS',research_halt_bypass=bool(maintenance and engine['engine_halted']))
        return loop.get_task(key)


def ack_preserving_overrun(loop,task,result,seconds,success=True):
    current=loop.get_task(task['task_key'])
    if current.get('overrun_receipt_json'):
        return dict(status='RECONCILIATION_REQUIRED',task_key=task['task_key'],reservation_retained=True)
    try:
        loop.ack(task['task_key'],task['claim_token'],result=result,actual_cash_units=0,actual_time_seconds=seconds,succeeded=success)
    except Exception:
        if loop.get_task(task['task_key']).get('overrun_receipt_json'):
            return dict(status='RECONCILIATION_REQUIRED',task_key=task['task_key'],reservation_retained=True)
        raise
    return dict(status='ACKNOWLEDGED',task_key=task['task_key'])

def perform_maintenance(s,loop,p,task,action):
    old=s.get('alpha_worker_receipt',task['task_key'],True)
    if old:
        return old,ack_preserving_overrun(loop,task,old['result'],old['elapsed_seconds'],old['success'])
    at=time.monotonic();ok=True
    try:
        case=s.get('alpha_case',action['case_id'])
        if action['operation']=='RESOLVE':result=alpha.resolve_case(s,p['config'],case)
        elif action['operation']=='ADVANCE':result=advance_with_quote_gate(s,p['config'],case,task['task_key'])
        else:
            alpha.maintain_exits(s,p['config'],case,s.get('alpha_snapshot',case['snapshot_id']));result=dict(status='POSITION_OBSERVED')
    except Exception as exc:ok=False;result=dict(status='MAINTENANCE_ERROR',error=str(exc))
    elapsed=math.ceil(time.monotonic()-at)
    receipt=s.append('alpha_worker_receipt',task['task_key'],dict(id=task['task_key'],operation=action['operation'],input_hash=task['task']['input_hash'],result=result,output_hash=digest(result),elapsed_seconds=elapsed,success=ok,at=utc()))
    return receipt,ack_preserving_overrun(loop,task,result,elapsed,ok)


def alpha_service(root):
    """Reuse sealed decisions/outcomes; never consume another owner's claim."""
    root=Path(root).resolve()
    with process_lock(root):
        s=Store(root);loop=None;started=time.monotonic();before={r['id'] for r in s.all('http_receipt')};flow_before=len(s.all('flow_window'))
        try:
            from .alpha_loop.audit import audit_runtime
            audit_runtime(root)
            p=alpha.verify_authority(s);c=p['config'];loop=alpha.open_loop(root,c)
            actions={r['id']:r for r in s.all('alpha_task_action')};blocked=[];performed=[]
            # Recover only this worker's tasks. Foreign and overrun claims are
            # visible in the shared authority and retained, not hidden or reset.
            for row in loop.db.execute("SELECT task_key FROM tasks WHERE state='CLAIMED'").fetchall():
                task=loop.get_task(row['task_key'])
                if task['task_key'] not in actions or task.get('overrun_receipt_json'):
                    blocked.append(task['task_key']);continue
                try:
                    action=actions[task['task_key']]
                    if action['operation'] in ('RESOLVE','MAINTAIN','ADVANCE'):
                        receipt,status=perform_maintenance(s,loop,p,task,action);performed.append(receipt['id'])
                        if status['status']!='ACKNOWLEDGED':blocked.append(task['task_key'])
                    else:performed.append(alpha.perform_task(s,loop,p,task,action)['id'])
                except Exception as exc:
                    s.failure('owned_alpha_recovery',exc,task_key=task['task_key']);blocked.append(task['task_key'])
            cases=s.all('alpha_case');pending=[x for x in cases if not s.get('alpha_outcome',x['id'],True)]
            # Position polling is allowed maintenance of already registered cases.
            # Its time is debited separately when it makes actual HTTP requests.
            maintenance_keys=[]
            for case in pending:
                if instant(utc())>=instant(case['due_at']):
                    key='resolve:'+case['id']+':'+utc()[:16]
                    alpha.enqueue(loop,s,alpha.claim_id(s,'semantic'),key,'due_label',dict(operation='RESOLVE',case_id=case['id'],attempt_minute=utc()[:16]),case['due_at'])
                    maintenance_keys.append(key)
                elif any(e['case_id']==case['id'] and e['status']=='FILLED' and not s.get('alpha_exit',e['id'],True) for e in s.all('alpha_entry_result')):
                    key='maintain:'+case['id']+':'+utc()[:16]
                    alpha.enqueue(loop,s,alpha.claim_id(s,'semantic'),key,'due_label',dict(operation='MAINTAIN',case_id=case['id'],attempt_minute=utc()[:16]),case['starts_at'])
                    maintenance_keys.append(key)
            actions={r['id']:r for r in s.all('alpha_task_action')}
            for _ in range(len(maintenance_keys)):
                task=claim_owned(loop,maintenance_keys,critical=True)
                if not task:break
                receipt,status=perform_maintenance(s,loop,p,task,actions[task['task_key']])
                performed.append(task['task_key'])
                if status['status']=='RECONCILIATION_REQUIRED':blocked.append(task['task_key'])
            # Healthy research uses the same frozen Alpha functions and caps.
            halted=loop.engine_status('CASHFLOW')['engine_halted']
            pending=[x for x in s.all('alpha_case') if not s.get('alpha_outcome',x['id'],True)]
            if not halted and not blocked:
                for case in pending:
                    incomplete=any(not s.get('alpha_entry_result',case['id']+':'+arm+':'+track,True) for arm in c['arms'] for track in ('NATIVE','MATCHED'))
                    quote_terminal=s.get('alpha_quote_terminal',case['id'],True)
                    if incomplete and not quote_terminal and instant(utc())<instant(case['due_at']):
                        action=advance_action(s,c,case);key='advance:'+case['id']+':'+digest(action)[:20]
                        alpha.enqueue(loop,s,alpha.claim_id(s,'semantic'),key,'research',action)
            if not pending and not halted and not blocked and instant(utc())<instant(p['new_case_deadline']):
                prefix='batch-'+str(len(cases)+1).zfill(5)+'-'+instant(utc()).strftime('%Y%m%d%H')
                attempts=loop.db.execute('SELECT task_key,state FROM tasks WHERE task_key LIKE ?',('new:'+prefix+'%',)).fetchall()
                if cases and len(attempts)<c['max_model_attempts'] and not any(t['state'] in ('QUEUED','CLAIMED') for t in attempts):
                    key=prefix+'-a'+str(len(attempts)+1)
                    alpha.enqueue(loop,s,alpha.claim_id(s,'semantic'),'new:'+key,'research',dict(operation='NEW_CASE',case_id=key))
            actions={r['id']:r for r in s.all('alpha_task_action')}
            if not blocked:
                for _ in range(3):
                    keys=[k for k,a in actions.items() if a['operation']!='MAINTAIN']
                    task=claim_owned(loop,keys)
                    if not task:break
                    try:
                        action=actions[task['task_key']]
                        if action['operation']=='ADVANCE':
                            receipt,status=perform_maintenance(s,loop,p,task,action);performed.append(receipt['id'])
                            if status['status']!='ACKNOWLEDGED':blocked.append(task['task_key'])
                        else:performed.append(alpha.perform_task(s,loop,p,task,action)['id'])
                    except Exception as exc:s.failure('owned_alpha_task',exc,task_key=task['task_key']);blocked.append(task['task_key'])
            evidence=alpha.submit(s,loop)
            result=dict(id=uuid.uuid4().hex,at=utc(),status='MAINTENANCE_SERVED_RESEARCH_BLOCKED' if blocked or halted else 'ALPHA_SERVED',
                        unresolved_claims=blocked,performed_tasks=performed,registered_cases=len(s.all('alpha_case')),matured_cases=len(s.all('alpha_outcome')),
                        http_receipt_ids=[r['id'] for r in s.all('http_receipt') if r['id'] not in before],elapsed_seconds=time.monotonic()-started,
                        flow_windows_added=len(s.all('flow_window'))-flow_before,
                        evidence=evidence,live_orders=0,base_source_hash=p['implementation_hash'])
            return s.append('alpha_service_receipt',result['id'],result)
        finally:
            if loop:loop.close()
            s.close()
