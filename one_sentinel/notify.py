"""Durable outbox. No external provider is configured or invoked by this module."""
from .store import digest

def risk_outbox(ledger, decision, holding_qty, observed_at):
    """Called in the decision transaction. One alert per semantic risk transition."""
    d=decision
    risky=d['action'] in ('EXIT','REDUCE') or d['reason']=='DATA_GAP_RETAIN_INVENTORY'
    rows=[r for r in ledger.all('risk_state') if r['symbol']==d['symbol']]
    previous=rows[-1] if rows else None
    signature=[d['action'],d['reason'],holding_qty] if risky else ['CLEAR']
    if previous and previous['signature']==signature:return None
    if not risky and not previous:return None
    revision=len(rows)+1;key=digest([d['symbol'],revision,signature])
    ledger.add('risk_state',key,dict(id=key,symbol=d['symbol'],signature=signature,
        revision=revision,decision_id=d['id'],at=d['at'],observed_at=observed_at))
    if not risky:return None
    return ledger.add('outbox',key,dict(id=key,event_id=d['event_id'],kind='RISK',
        origin='DETERMINISTIC_RULE',revision=revision,received_at=observed_at,created_at=d['at'],
        body=dict(decision_id=d['id'],evidence_ids=d['input_ids'],risk_reason=d['reason'],
                  action=d['action'],symbol=d['symbol'],holding_qty=holding_qty,
                  classification=d['classification']),phone_received_at=None))

def enqueue(ledger,event,kind,at,revision=0,body=None):
    key=digest([event['id'],kind,revision])
    old=ledger.get('outbox',key)
    if old:return old
    return ledger.add('outbox',key,dict(id=key,event_id=event['id'],kind=kind,origin='AI' if kind=='AI_ANALYSIS' else 'DETERMINISTIC_RULE',revision=revision,received_at=event['at'],created_at=at,body=body if body is not None else {k:event.get(k) for k in ('symbol','reason','receipt_id','classification')},phone_received_at=None))

def dispatch_synthetic(ledger,now):
    """Fake provider ONLY. ACK is explicitly not actual delivery/phone receipt."""
    with ledger.transaction():
        for r in ledger.all('outbox'):
            if not ledger.get('delivery',r['id']):
                ledger.add('delivery',r['id'],dict(id=r['id'],provider='SYNTHETIC_PROVIDER',ack_at=now,latency_seconds=now-r['received_at'],phone_received_at=None,actual_delivery=False))

def queue_candidate(ledger,policy,event,category,at):
    # One event/category slot; repeated input merges without another opportunity.
    key=digest([event['id'],category]);old=ledger.get('candidate',key)
    if old:return old
    active=[r for r in ledger.all('candidate') if not ledger.get('analysis_result',r['id']) and r['expires_at']>=at and r['status']=='QUEUED']
    return ledger.add('candidate',key,dict(id=key,event_id=event['id'],category=category,at=at,expires_at=at+policy['model_max_wait_seconds'],status='UNREVIEWED_QUEUE_FULL' if len(active)>=policy['model_queue_capacity'] else 'QUEUED'))

def analyze_one(ledger,policy,now,mode='DISABLED',answer=None):
    """Offline scheduling contract. No paid provider; timeout/answer are fixtures."""
    with ledger.transaction():
        candidates=[r for r in ledger.all('candidate') if r['status']=='QUEUED' and not ledger.get('analysis_result',r['id'])]
        candidates.sort(key=lambda r:(-(now-r['at'])//300,{'HOLDINGS':0,'NEW':1,'STRUCTURAL':2}[r['category']],r['id']))
        for r in candidates:
            key=r['id']
            if r['at']>now:continue
            if now>r['expires_at']:
                ledger.add('analysis_result',key,dict(id=key,at=now,status='UNREVIEWED_EXPIRED'));continue
            if mode=='DISABLED':
                ledger.add('analysis_block',digest([key,now,'DISABLED']),dict(id=key,at=now,status='UNREVIEWED_MODEL_DISABLED'))
                return dict(status='UNREVIEWED_MODEL_DISABLED',candidate_id=key)
            if mode not in ('SYNTHETIC_TIMEOUT','SYNTHETIC_ANSWER'):raise ValueError('NO_AUTHORIZED_PROVIDER')
            attempts=[a for a in ledger.all('analysis_attempt') if a['at']>=now-86400]
            if len(attempts)>=policy['model_daily_attempts'] or sum(a['category']==r['category'] for a in attempts)>=policy['model_allocation'][r['category']]:
                ledger.add('analysis_block',digest([key,now]),dict(id=key,at=now,status='UNREVIEWED_BUDGET'));continue
            ledger.add('analysis_attempt',key,dict(id=key,at=now,category=r['category'],synthetic=True,cash_cost='0'))
            status='UNREVIEWED_TIMEOUT' if mode=='SYNTHETIC_TIMEOUT' else 'SYNTHETIC_REVIEWED'
            result=ledger.add('analysis_result',key,dict(id=key,at=now,status=status,answer=answer if mode=='SYNTHETIC_ANSWER' else None))
            if mode=='SYNTHETIC_ANSWER':
                event=ledger.get('event',r['event_id']);enqueue(ledger,event,'AI_ANALYSIS',now,1,answer)
            return result
        return dict(status='NO_ELIGIBLE_ANALYSIS')
