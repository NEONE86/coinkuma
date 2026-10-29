"""Fair native-time arms and bounded, non-resampled real model calls."""
import json
from datetime import timedelta
from one_trading.autonomy.model import invoke
from one_trading.autonomy.store import utc, instant, digest
from one_trading.autonomy.market import number

SCHEMA={"type":"object","properties":{
    "action":{"type":"string","enum":["LONG","NO_TRADE"]},
    "symbol":{"type":"string"},
    "classification":{"type":"string","enum":["TEMPORARY_FLOW","INFORMATION_CONTINUATION","UNKNOWN"]},
    "p_reversion":{"type":"number","minimum":0,"maximum":1},
    "observed":{"type":"string"},"source_claim":{"type":"string"},
    "assumptions":{"type":"string"},"counter_hypothesis":{"type":"string"},
    "invalidation":{"type":"string"},"rationale":{"type":"string"}},
    "required":["action","symbol","classification","p_reversion","observed","source_claim",
                "assumptions","counter_hypothesis","invalidation","rationale"],"additionalProperties":False}
PROMPT="""You are the current-information economic arm of a frozen forward experiment.
Use ONLY the supplied data; no external knowledge, files, web, tools, or unreceived outcomes.
Bybit liquidation side has been normalized: forced SELL is a liquidated long. Its bankruptcy
price is NOT an executable Binance spot price. Spillover between venues is an ASSUMPTION.
Distinguish temporary inventory shock from information-led continuation. Price decline alone
does not prove forced selling. Inspect the full universe, not a preferred shortlist.
Return one LONG or NO_TRADE decision with explicit observed/source-claim/assumption separation,
a counter-hypothesis and falsifiable invalidation. LONG only for a negative residual candidate
with actually received forced SELL evidence and a temporary-flow classification. Shared numeric
and post-delay executable-gap/risk checks are applied by code. Probability is uncalibrated and
does not set size. No leverage, short, order API, or real trading. If memory is supplied, it
contains only previously matured and independently checked events. Do not invent memory.
"""

def validate_answer(answer,universe):
    if not isinstance(answer,dict) or set(answer)!=set(SCHEMA["required"]):raise ValueError("MODEL_SCHEMA")
    if answer['action'] not in ('LONG','NO_TRADE'):raise ValueError('MODEL_ACTION')
    if answer['symbol'] not in universe+['CASH']:raise ValueError('MODEL_SYMBOL')
    if answer['classification'] not in ('TEMPORARY_FLOW','INFORMATION_CONTINUATION','UNKNOWN'):raise ValueError('MODEL_CLASS')
    if isinstance(answer['p_reversion'],bool) or not isinstance(answer['p_reversion'],(float,int)) or not 0<=number(answer['p_reversion'])<=1:raise ValueError('MODEL_PROBABILITY')
    for k in set(SCHEMA['required'])-{'p_reversion'}:
        if not isinstance(answer[k],str) or not 1<=len(answer[k])<=5000:raise ValueError('MODEL_TEXT')
    if answer['action']=='LONG' and (answer['symbol']=='CASH' or answer['classification']!='TEMPORARY_FLOW'):raise ValueError('MODEL_INCONSISTENT_LONG')

def price_decision(snapshot,config):
    candidates=[];rejected={}
    for s in snapshot['universe']:
        x=snapshot['state'].get(s)
        if not x:rejected[s]='DATA_BLOCKED';continue
        if abs(x['z'])<config['entry_abs_z']:rejected[s]='NO_EXTREME_RESIDUAL';continue
        if x['z']>0:rejected[s]='SHORT_UNSUPPORTED';continue
        candidates.append((abs(x['z']),s))
    selected=max(candidates,default=(0,'CASH'))[1]
    return dict(action='LONG' if selected!='CASH' else 'NO_TRADE',symbol=selected,
                status='DECIDED',ready_at=utc(),input_hash=digest(snapshot),
                ranked_candidates=[s for _,s in sorted(candidates,reverse=True)],
                rejected_alternatives=rejected,information_scope='PRICE_ONLY_FULL_UNIVERSE')

def retrieve_memory(store,cutoff,limit=20):
    records=[]
    for m in store.all('event_memory'):
        if m['origin']!='FORWARD_PAPER' or not m['independent_audit_passed']:continue
        if any(instant(m[k])>instant(cutoff) for k in ['due_at','received_at','stored_at']):continue
        records.append(m)
    # Entire nonoverlapping market batches, including losses and abstentions.
    return sorted(records,key=lambda m:m['due_at'])[-limit:]

def model_decision(store,config,case,arm,snapshot,flow):
    key=case['id']+':'+arm
    old=store.get('alpha_decision',key,True)
    if old:return old
    memory=retrieve_memory(store,snapshot['cutoff']) if arm=='CF_SEMANTIC_MEMORY' else []
    payload=dict(snapshot=snapshot,flow=flow,memory=memory,
                 memory_status='AVAILABLE' if memory else 'MEMORY_UNAVAILABLE',
                 contract={k:config[k] for k in ['horizon_seconds','fee_bps_per_side','slippage_bps_per_side','entry_abs_z','stop_fraction']})
    blocked=None
    if snapshot['errors']:blocked='DATA_BLOCKED'
    elif not flow['events']:blocked='DATA_BLOCKED_NO_OBSERVED_FORCED_FLOW'
    elif arm=='CF_SEMANTIC_MEMORY' and not memory:blocked='MEMORY_UNAVAILABLE'
    if blocked:
        return store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,status=blocked,
                action=None,symbol=None,ready_at=utc(),input_hash=digest(payload),memory_ids=[],model_call_ids=[]))
    calls=[]
    for attempt in range(config['max_model_attempts']):
        aid=digest([key,'attempt',attempt+1])
        call=store.get('model_call',aid,True)
        if call is None:
            if instant(utc())>instant(snapshot['cutoff'])+timedelta(seconds=config['decision_validity_seconds']):break
            intent=store.get('alpha_model_intent',aid,True)
            if intent:
                # A durable completed receipt can reconcile an interrupted DB write.
                files=list((store.root/'models'/aid).glob('*/completed.json'))
                if len(files)==1:
                    receipt=json.loads(files[0].read_text(encoding='utf-8'))
                    if receipt['input_hash']!=digest(payload):raise ValueError('RECOVERY_MODEL_INPUT')
                    call=store.append('model_call',aid,receipt)
                else:
                    return store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,
                        status='MODEL_EXTERNAL_STATE_UNCERTAIN',action=None,symbol=None,ready_at=utc(),
                        input_hash=digest(payload),memory_ids=[m['id'] for m in memory],model_call_ids=calls))
            else:
                if attempt and (instant(utc())-instant(store.get('model_call',calls[-1])['completed_at'])).total_seconds()<config['model_retry_seconds'][attempt-1]:
                    return None  # bounded retry on a later worker tick, no blocking sleep
                store.append('alpha_model_intent',aid,dict(id=aid,input_hash=digest(payload),started_at=utc()))
                call=invoke(store,config,aid,payload,schema=SCHEMA,instruction=PROMPT,
                            validator=lambda a:validate_answer(a,config['universe']))
        calls.append(aid)
        if call['status']=='CONNECTED_VALID':
            a=call['answer'];symbol=a['symbol'];action=a['action'];reason=None
            if action=='LONG' and (snapshot['state'][symbol]['z']>=-config['entry_abs_z'] or not any(
                    e['symbol']==symbol and e['forced_side']=='SELL' for e in flow['events'])):
                action='NO_TRADE';reason='REGISTERED_CAUSAL_OR_NUMERIC_GATE'
            return store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,status='DECIDED',
                 action=action,symbol=symbol,ready_at=call['completed_at'],input_hash=digest(payload),
                 answer=a,gate_reason=reason,memory_ids=[m['id'] for m in memory],model_call_ids=calls))
    return store.append('alpha_decision',key,dict(id=key,case_id=case['id'],arm=arm,status='MODEL_BLOCKED',
            action=None,symbol=None,ready_at=utc(),input_hash=digest(payload),
            memory_ids=[m['id'] for m in memory],model_call_ids=calls))
