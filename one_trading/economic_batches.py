"""Bounded economic exploration beside, never inside, sealed Alpha cohorts."""
import argparse,hashlib,json,math,re,sqlite3,time,uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,canonical,atomic_bytes,process_lock
from .autonomy.market import PublicClient,SPOT,FUTURES,fresh_book
from .autonomy.model import invoke
from .economic_research.chain_capture import ChainCapture
from .economic_research.evidence import VersionedEvidence
from .alpha_loop.protocol import defaults,source_hash
from .alpha_loop.worker import open_loop
from .alpha_loop.reference.research_loop import ExperimentSpec
from .alpha_loop.trading import fill
from .batch_analysis import design_diagnostic,historical_contexts,historical_comparison,credit_gate
from .batch_control import claim_owned,ack_preserving_overrun
from .batch_models import INSTRUCTION,frozen_payload,request_contract,validate_artifacts,policy_costs

FAMILIES={
 'forced_flow':('CASHFLOW','Received forced liquidations may temporarily displace spot price beyond executable costs.'),
 'funding_crowding':('CASHFLOW','Negative perp financing and prior spot weakness may identify less crowded long rebounds.'),
 'credit_growth':('CAPITAL_GROWTH','Expanding Ethereum USDC supply and Aave credit demand may precede incremental holder demand.')}
ADDRESS_URL='https://raw.githubusercontent.com/aave-dao/aave-address-book/main/src/AaveV3Ethereum.sol'
ADDRESSES={'usdc_supply':'0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
 'aave_usdc_deposits':'0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c',
 'aave_usdc_debt':'0x72E95b8931767C79bA4EeE721354d6E99a61D004'}

def implementation_hash():
    h=hashlib.sha256(source_hash().encode())
    for name in ('batch_analysis.py','economic_batches.py','batch_control.py','batch_models.py','hypothesis_loop.py'):h.update((Path(__file__).parent/name).read_bytes())
    return h.hexdigest()

def readonly_rows(root,kind):
    p=Path(root)/'research.sqlite3';c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        out=[]
        for body,h in c.execute('SELECT body,sha256 FROM records WHERE kind=? ORDER BY sequence',(kind,)):
            if hashlib.sha256(body.encode()).hexdigest()!=h:raise ValueError('SOURCE_LEDGER_HASH')
            out.append(json.loads(body))
        return out
    finally:c.close()

def initialize(root,alpha):
    with process_lock(root):
        s=Store(root)
        try:
            old=s.get('batch_config','frozen',True)
            if old:return old
            p=readonly_rows(alpha,'alpha_protocol')[0]
            if p['implementation_hash']!=source_hash():raise ValueError('BASE_SOURCE_DIFFERS')
            c=dict(id='frozen',registered_at=utc(),alpha_runtime=str(Path(alpha).resolve()),
                authority_id=p['authority_id'],source_hash=implementation_hash(),base_source_hash=source_hash(),
                max_trials_per_batch=3,max_model_calls_per_batch=2,max_trial_seconds=240,
                max_batch_seconds=360,max_batches_per_day=1,max_http_requests_per_batch=90,
                batch_interval_hours=24,research_days=14,information_cost_usdt_per_call=.1,
                execution_contract_version=2,growth_decision_validity_seconds=600,
                information_cost_status='SENSITIVITY_ASSUMPTION_NOT_MEASURED_PROVIDER_BILL',
                no_new_paid_services=True,no_live_orders=True,capital_usdt=10000,
                growth_capital_krw=8000000,fx_krw_per_usdt=1400,growth_capital_usdt=8000000/1400,
                capital_scope='Separate counterfactual sleeves, never summed or funded; retain prior growth cap and fixed FX assumption.',
                funding_historical_days=30,growth_hold_days=30,growth_confirmation_spacing_seconds=86400,
                growth_predicate='Ethereum USDC totalSupply and Aave v3 Ethereum variable USDC debt totalSupply both increase between two fixed block states; stock proxy, not attributable net flow or holder capture.',
                alpha_allocation='NONE_EXPLORATION_NO_SIGNIFICANCE_PROMOTION',schedule_status='NOT_INSTALLED_BY_INIT')
            return s.append('batch_config','frozen',c)
        finally:s.close()

def revise_implementation(root,reason):
    """Append an operational code revision; never edit a historical config/result."""
    if not reason:raise ValueError('REVISION_REASON_REQUIRED')
    with process_lock(root):
        s=Store(root)
        try:
            old=s.all('batch_config')[-1]
            if old['source_hash']==implementation_hash():return old
            if any(not s.get('batch_result',b['id'],True) for b in s.all('research_batch')):raise ValueError('UNFINISHED_BATCH_CANNOT_REVISE')
            c={k:v for k,v in old.items() if k!='stored_at'}
            c.update(id='implementation-'+implementation_hash()[:12],source_hash=implementation_hash(),
                     prior_config_hash=digest(old),revision_at=utc(),revision_reason=reason)
            c.update(execution_contract_version=2,growth_decision_validity_seconds=600,
                     new_decision_contract_effective_at=utc())
            return s.append('batch_config',c['id'],c)
        finally:s.close()

class BoundedInputs:
    def __init__(self,store,batch_id,config):
        self.store=store;self.batch=batch_id;self.config=config;self.started=time.monotonic()
        self.used=sum(r['elapsed_seconds'] for r in store.all('trial_receipt') if r['id'].startswith(batch_id+':'))+sum(r['elapsed_seconds'] for r in store.all('hypothesis_receipt') if r['request_id']==batch_id)
        self.trial_started=None
    def start_trial(self):self.trial_started=time.monotonic()
    def remaining(self):
        now=time.monotonic();batch=self.config['max_batch_seconds']-self.used-(now-self.started)
        trial=self.config.get('max_trial_seconds',240)-(now-self.trial_started) if self.trial_started is not None else batch
        return min(batch,trial)
    def check(self,reserve_seconds=0):
        b=self.store.get('research_batch',self.batch)
        if self.remaining()<reserve_seconds:raise ValueError('BATCH_ACTIVE_TIME_LIMIT_OR_TRIAL_DEADLINE')
        receipts=self.store.all('http_receipt')+self.store.all('retrieval_receipt')
        n=sum(instant(r['received_at'])>=instant(b['started_at']) for r in receipts)
        # Existing HTTP client retries at most three times; reserve that ceiling.
        if n+3>self.config['max_http_requests_per_batch']:raise ValueError('BATCH_REQUEST_LIMIT')
    def cached(self,key,fn):
        key=self.batch+':'+key;old=self.store.get('batch_input',key,True)
        if old:return old['value']
        self.check();value=fn()
        self.store.append('batch_input',key,dict(id=key,value=value,received_at=utc(),content_hash=digest(value)))
        return value
    def get(self,host,path,params):
        def request():
            self.check(50) # three bounded 15s attempts plus backoff/margin
            data,r=PublicClient(self.store).get(host,path,params);return dict(data=data,receipt=r)
        return self.cached(digest([host,path,params]),request)
    def rpc(self,method,params):
        def request():
            self.check(30)
            data,r=ChainCapture(self.store).rpc(method,params);return dict(data=data,receipt=r)
        return self.cached(digest([method,params]),request)

def model_once(s,c,bid,family,payload,schema,validator,inputs=None):
    key=digest([bid,family,'model-v1']);old=s.get('model_call',key,True)
    intent=s.get('batch_model_intent',key,True)
    prior_request=s.get('batch_model_request',key,True)
    if (intent or old) and not prior_request:
        # Legacy incomplete intent lacks a frozen contract: preserve uncertainty,
        # do not infer a new request or spend again.
        if old and old['input_hash']!=digest(payload):raise ValueError('CACHED_MODEL_IDENTITY_MISMATCH')
        return dict(id=key,status='LEGACY_REQUEST_UNVERIFIED' if old else 'EXTERNAL_STATE_UNCERTAIN',answer=None,started_at=(old or intent)['started_at'],completed_at=(old or {}).get('completed_at',utc()))
    config=defaults();config.update(model_timeout_seconds=90,model_prompt_version='economic-batch-v2-frozen-request')
    request=request_contract(s,key,payload,schema,config)
    if old:return validate_artifacts(s,request,old,validator)
    if intent:
        completed=list((s.root/'models'/key).glob('*/completed.json'))
        if len(completed)==1:
            record=json.loads(completed[0].read_text(encoding='utf-8'))
            validate_artifacts(s,request,record,validator)
            return s.append('model_call',key,record)
        return dict(id=key,status='EXTERNAL_STATE_UNCERTAIN',answer=None,started_at=intent['started_at'],completed_at=utc())
    calls=[r for r in s.all('batch_model_intent') if r['batch_id']==bid]
    if len(calls)>=c['max_model_calls_per_batch']:raise ValueError('BATCH_MODEL_LIMIT')
    if inputs:inputs.check(config['model_timeout_seconds']+5)
    s.append('batch_model_intent',key,dict(id=key,batch_id=bid,started_at=utc(),input_hash=digest(payload)))
    record=invoke(s,request['config'],key,request['payload'],schema=request['schema'],instruction=request['instruction'],validator=validator)
    return validate_artifacts(s,request,record,validator)

def schema():
    fields={'id':{'type':'string'},'symbol':{'type':'string'},'action':{'type':'string','enum':['LONG','NO_TRADE']},
        'observed':{'type':'string'},'inference':{'type':'string'},'counter_hypothesis':{'type':'string'},'next_falsifier':{'type':'string'}}
    return {'type':'object','properties':{'decisions':{'type':'array','items':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}},'required':['decisions'],'additionalProperties':False}

def validate(answer,ids,universe):
    if not isinstance(answer,dict) or set(answer)!={'decisions'}:raise ValueError('MODEL_SCHEMA')
    ds=answer['decisions'];fields=set(schema()['properties']['decisions']['items']['required'])
    if not isinstance(ds,list) or len(ds)!=len(ids) or sorted(d.get('id','') for d in ds)!=sorted(ids):raise ValueError('MODEL_CASE_IDENTITY')
    for d in ds:
        if set(d)!=fields or d['action'] not in ('LONG','NO_TRADE') or d['symbol'] not in universe:raise ValueError('MODEL_ASSET_ACTION')
        if any(not isinstance(d[k],str) or not d[k] for k in fields):raise ValueError('MODEL_FIELDS')

def selection_schema():
    props={k:{'type':'string'} for k in ('observed','inference','counter_hypothesis','next_falsifier')}
    props['rule']={'type':'string','enum':['PRICE_ONLY','FUNDING_RULE','CASH']}
    return dict(type='object',properties=props,required=list(props),additionalProperties=False)

def validate_selection(answer):
    if not isinstance(answer,dict) or set(answer)!=set(selection_schema()['required']):raise ValueError('MODEL_SELECTION_SCHEMA')
    if answer['rule'] not in ('PRICE_ONLY','FUNDING_RULE','CASH') or any(not isinstance(v,str) or not v for v in answer.values()):raise ValueError('MODEL_SELECTION')

def apply_selected_rule(contexts,rule):
    decisions=[]
    for c in contexts:
        ranked=sorted(c['assets'],key=lambda s:(c['assets'][s]['past_24h_return'],s))
        choices=[s for s in ranked if c['assets'][s]['past_24h_return']<0 and
                 (rule=='PRICE_ONLY' or (rule=='FUNDING_RULE' and c['assets'][s]['last_three_funding_sum']<0))]
        decisions.append(dict(id=c['id'],symbol=choices[0] if choices else ranked[0],action='LONG' if choices else 'NO_TRADE'))
    return decisions

def forced_flow(s,c,bid,inputs):
    alpha=Path(c['alpha_runtime']);snapshots=readonly_rows(alpha,'alpha_snapshot');cases=readonly_rows(alpha,'alpha_case')
    entries=readonly_rows(alpha,'alpha_entry_result');decisions=readonly_rows(alpha,'alpha_decision')
    packet=inputs.cached('forced-prior',lambda:dict(snapshots=snapshots,cases=cases,entries=entries,decisions=decisions,
         original_received=True,read_at=utc(),scope='REUSE_EXISTING_FORWARD_OBSERVATION_NOT_NEW_INDEPENDENT_UNIT'))
    checks=[]
    for snap in packet['snapshots']:
        for symbol,state in snap['state'].items():
            b=snap['books'][symbol];gap=state['reference_price']/b['asks'][0][0]-1
            cost=.003+b['spread_bps']/10000
            checks.append(dict(symbol=symbol,snapshot_id=snap['id'],gap=gap,assumed_cost=cost,above_cost=gap>cost))
    return dict(status='REJECTED' if checks and not any(r['above_cost'] for r in checks) else 'MORE_OBSERVATION',
        rejection_scope='Immediate long opportunity in this observed window; not every future forced-flow event.',
        input_hash=digest(packet),checks=checks,independent_forward_units=0,paper_trades=0,
        pnl=None,reason='Reuse cannot create an independent result; current micro-displacement must exceed costs.',
        comparison=packet['entries'],next_hypothesis='Test stronger actual cause observations using the unchanged Alpha cohort; do not lower costs to manufacture entries.',
        next_due=(instant(utc())+timedelta(hours=24)).isoformat())

def funding_crowding(s,c,bid,inputs):
    end=int(instant(s.get('research_batch',bid)['started_at']).timestamp()//3600)*3600000
    data={};sources={}
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        bars=inputs.get(SPOT,'/api/v3/klines',dict(symbol=symbol,interval='1h',startTime=end-32*24*3600000,endTime=end-1,limit=1000))
        funding=inputs.get(FUTURES,'/fapi/v1/fundingRate',dict(symbol=symbol,startTime=end-33*24*3600000,endTime=end-1,limit=1000))
        rows=bars['data'];expected=list(range(end-32*24*3600000,end,3600000))
        if [r[0] for r in rows]!=expected or any(r[6]!=r[0]+3599999 for r in rows):raise ValueError('HISTORICAL_BAR_COVERAGE')
        if any(r['symbol']!=symbol for r in funding['data']):raise ValueError('FUNDING_ASSET')
        data[symbol]=dict(bars=rows,funding=funding['data']);sources[symbol]=[bars['receipt'],funding['receipt']]
    past=[r for r in s.all('trial_result') if r['family']=='funding_crowding']
    if past:
        # The failed/adverse explored rule is not silently tuned or retested as
        # independent evidence. Accumulate a truly received future feature panel.
        return dict(status='MORE_OBSERVATION',input_hash=digest(data),source_receipts=sources,paper_trades=0,
            next_operation='COLLECT_UNUSED_FUNDING_PANEL',new_investment_trials=0,independent_forward_units=0,
            reason='Previous exploration preserved; newly received feature panel only, no repeated backtest or adopted failed rule.',
            next_hypothesis='After a distinct future panel matures, preregister a new financing mechanism before comparing its returns.',
            next_due=(instant(utc())+timedelta(days=1)).isoformat())
    contexts=historical_contexts(data)
    if not contexts:return dict(status='DATA_INSUFFICIENT',missing=['Matched complete funding/price windows'],paper_trades=0,next_due=(instant(utc())+timedelta(days=1)).isoformat())
    payload=frozen_payload(s,bid+':funding-payload',lambda:dict(scope='RETROSPECTIVE_RECONSTRUCTION_NOT_FORWARD_AI_EVIDENCE',cutoff=utc(),earliest_context=contexts[0],
                 task='Choose one fixed rule for the whole diagnostic without outcomes or later contexts. PRICE_ONLY buys most negative past-24h asset if negative. FUNDING_RULE additionally requires negative sum of last three settled funding payments. CASH stays cash. This is a single AI rule selection, not per-case AI inference.',
                 historical_availability='Downloaded now; assumed event publication then, not actually received then.',
                 costs=dict(capital=10000,exposure=.125,per_side=.0016,model_call=.1),source_hashes={z:[r['sha256'] for r in rs] for z,rs in sources.items()}))
    call=model_once(s,c,bid,'funding_crowding',payload,selection_schema(),validate_selection,inputs)
    ai=apply_selected_rule(contexts,call['answer']['rule']) if call['status']=='CONNECTED_VALID' else None
    result=historical_comparison(contexts,data,ai,information_cost=c['information_cost_usdt_per_call'])
    economic=result['aggregates']['FUNDING_RULE'];price=result['aggregates']['PRICE_ONLY']
    result.update(status='REJECTED' if economic['net_usdt']<=max(0,price['net_usdt']) else 'NEXT_VALIDATION',
       input_hash=digest(data),source_receipts=sources,contexts=contexts,model_call_id=call['id'],model_status=call['status'],
       ai_method='ONE_FIXED_RULE_SELECTION_FROM_EARLIEST_CONTEXT_ONLY_NO_FUTURE_CONTEXTS_IN_PROMPT',
       selected_ai_rule=call['answer']['rule'] if ai is not None else None,
       paper_trades=0,next_operation='COLLECT_UNUSED_FUNDING_PANEL',
       next_hypothesis=f"Observed funding-rule net {economic['net_usdt']:.6f} vs price {price['net_usdt']:.6f} USDT. Preserve this result; collect genuinely new funding features, not retune this sample. Retrospective AI is not forward evidence.",
       next_due=(instant(utc())+timedelta(days=1)).isoformat(),rejection_scope='Fixed negative-funding contrarian filter on this reused historical sample, not all financing mechanisms.')
    return result

def credit_observation(s,c,bid,inputs):
    try:
        def addresses():
            inputs.check(30)
            data,r=VersionedEvidence(s).get(ADDRESS_URL)
            if any(v.lower().encode() not in data.lower() for v in ADDRESSES.values()):raise ValueError('ADDRESS_BOOK_MISMATCH')
            return dict(receipt=r,addresses=ADDRESSES)
        addr=inputs.cached('aave-addresses',addresses)
        chain=inputs.rpc('eth_chainId',[])
        if chain['data']!='0x1':raise ValueError('CHAIN_ID')
        final=inputs.rpc('eth_getBlockByNumber',['finalized',False]);number=int(final['data']['number'],16)
        if int(final['data']['timestamp'],16)>instant(final['receipt']['received_at']).timestamp():raise ValueError('FUTURE_FINALIZED_BLOCK')
        points=[]
        for block in (number-7200,number):
            b=inputs.rpc('eth_getBlockByNumber',[hex(block),False]);values={};receipts=[]
            for key,address in ADDRESSES.items():
                code=inputs.rpc('eth_getCode',[address,hex(block)])
                if code['data']=='0x':raise ValueError('TOKEN_CODE_MISSING')
                decimals=inputs.rpc('eth_call',[dict(to=address,data='0x313ce567'),hex(block)])
                total=inputs.rpc('eth_call',[dict(to=address,data='0x18160ddd'),hex(block)])
                if int(decimals['data'],16)!=6:raise ValueError('DECIMALS')
                values[key]=int(total['data'],16)/1e6
                if not math.isfinite(values[key]) or values[key]<=0:raise ValueError('INVALID_STOCK')
                receipts += [code['receipt']['receipt_id'],decimals['receipt']['receipt_id'],total['receipt']['receipt_id']]
            check=ChainCapture(s);inputs.check(30);again,r=check.rpc('eth_getBlockByNumber',[hex(block),False])
            if again['hash']!=b['data']['hash'] or int(b['data']['number'],16)!=block:raise ValueError('CANONICAL_RECHECK')
            if int(b['data']['timestamp'],16)>instant(b['receipt']['received_at']).timestamp():raise ValueError('FUTURE_BLOCK')
            points.append(dict(number=block,hash=b['data']['hash'],timestamp=int(b['data']['timestamp'],16),values=values,
                              block_receipt=b['receipt']['receipt_id'],canonical_receipt=r['receipt_id'],value_receipts=receipts))
        return dict(status='MEASURED',observed_at=utc(),points=points,address_source=addr,
            scope='ETHEREUM_ONLY_HISTORICAL_STOCKS_RECEIVED_NOW_NOT_FLOWS_OR_PROGRAM_ATTRIBUTION')
    except Exception as exc:return dict(status='DATA_INSUFFICIENT',observed_at=utc(),missing=[type(exc).__name__+':'+str(exc)])

def growth_execution(s,c,key,decision,gate,market,info_cost,inputs=None):
    old=s.get('growth_bridge_result',key,True)
    if old:return old
    eligible=(instant(decision['ready_at'])+timedelta(seconds=2)).isoformat()
    base=dict(id=key,arm=decision['arm'],symbol='AAVEUSDT',decision=decision,gate=gate,
              eligible_at=eligible,information_cost_usdt=info_cost,fill=None)
    expiry=decision.get('expires_at')
    invalid_state=decision.get('gate_hash') is not None and decision['gate_hash']!=digest(gate)
    if (c.get('execution_contract_version',1)>=2 and not expiry) or (expiry and instant(utc())>instant(expiry)):
        base.update(status='EXPIRED',reason='FROZEN_DECISION_VALIDITY_ENDED_OR_LEGACY_EXPIRY_MISSING')
    elif invalid_state:base.update(status='INVALIDATED',reason='CAUSE_STATE_IDENTITY_CHANGED')
    elif decision['action'] is None:base.update(status='MODEL_UNAVAILABLE')
    elif decision['action']=='NO_TRADE':base.update(status='CASH',reason='POLICY_NO_TRADE')
    elif decision['arm']=='G_LAGGED' and not gate['allow_entry']:base.update(status='WAIT_ECONOMIC_CONFIRMATION',reason='NEED_DISTINCT_POST_REGISTRATION_DAILY_CONFIRMATIONS')
    else:
        if instant(utc())<instant(eligible):time.sleep(min(2,(instant(eligible)-instant(utc())).total_seconds()+.01))
        try:
            # Daily observations cannot multiply one policy's capital by opening
            # an unbounded stack of overlapping 30-day positions.
            active=[r for r in s.all('growth_bridge_result') if r['arm']==decision['arm'] and r['status']=='FILLED' and not s.get('growth_bridge_exit',r['id'],True)]
            if active:
                base.update(status='WAIT_EXISTING_POSITION',reason='ONE_POSITION_PER_POLICY_UNTIL_EXIT')
                return s.append('growth_bridge_result',key,base)
            if inputs:inputs.check(50)
            book=fresh_book(s,'AAVEUSDT')
            if expiry and instant(book['received_at'])>instant(expiry):
                base.update(status='EXPIRED',reason='QUOTE_ARRIVED_AFTER_FROZEN_EXPIRY');return s.append('growth_bridge_result',key,base)
            if instant(book['received_at'])<instant(eligible):raise ValueError('PRE_DECISION_BOOK')
            config=defaults();config['capital_usdt']=c['growth_capital_usdt'];quantity=config['capital_usdt']*.125/(book['asks'][0][0]*1.0015)
            f=fill(book,'BUY',config,quantity,market['filters'])
            base.update(status='FILLED',fill=f,exit_due=(instant(f['simulated_at'])+timedelta(days=c['growth_hold_days'])).isoformat(),
                        exit_policy='First executable quote at 30d or observed 2 percent adverse stop; polled not guaranteed.')
        except Exception as exc:base.update(status='DATA_INSUFFICIENT',reason=str(exc))
    return s.append('growth_bridge_result',key,base)

def credit_growth(s,c,bid,inputs):
    key=bid+':credit';ob=s.get('credit_observation',key,True)
    if ob is None:ob=s.append('credit_observation',key,dict(id=key,**credit_observation(s,c,bid,inputs)))
    gate=credit_gate(ob,s.all('credit_observation'),c['registered_at'])
    end=inputs.cached('credit-panel-cutoff',lambda:dict(end=int(instant(utc()).timestamp()//3600)*3600000))['end']
    bars=inputs.get(SPOT,'/api/v3/klines',dict(symbol='AAVEUSDT',interval='1h',startTime=end-25*3600000,endTime=end-1,limit=25))
    if len(bars['data'])!=25:raise ValueError('GROWTH_PRICE_COVERAGE')
    price_return=float(bars['data'][-1][4])/float(bars['data'][0][4])-1
    exchange=inputs.get(SPOT,'/api/v3/exchangeInfo',dict(symbol='AAVEUSDT'))
    symbol=exchange['data']['symbols'][0]
    if symbol['symbol']!='AAVEUSDT' or symbol['status']!='TRADING':raise ValueError('GROWTH_ASSET_NOT_TRADING')
    market={'filters':symbol['filters']}
    baseline=frozen_payload(s,bid+':price-decision',lambda:dict(id=bid+':G_PRICE_ONLY',arm='G_PRICE_ONLY',action='LONG' if price_return>0 else 'NO_TRADE',ready_at=utc(),
        expires_at=(instant(utc())+timedelta(seconds=c.get('growth_decision_validity_seconds',600))).isoformat(),gate_hash=digest(gate),input_hash=digest(bars)))
    # Native price baseline gets its executable decision before the AI call.
    results=[growth_execution(s,c,bid+':G_PRICE_ONLY',baseline,gate,market,0,inputs)]
    payload=frozen_payload(s,bid+':credit-payload',lambda:dict(scope='NEW_CURRENT_GROWTH_RESEARCH_NOT_OLD_90DAY_CONTRACT',cutoff=utc(),
        cases=[dict(id=key,symbol='AAVEUSDT',observation=ob,economic_gate=gate,past_24h_return=price_return)],
        holding_days=30,missing_value_capture='No measured AAVE holder revenue or attributable purchase; debt stocks include interest.'))
    call=model_once(s,c,bid,'credit_growth',payload,schema(),lambda a:validate(a,[key],['AAVEUSDT']),inputs)
    action=call['answer']['decisions'][0]['action'] if call['status']=='CONNECTED_VALID' else None
    for arm in ('G_AI','G_LAGGED','CASH'):
        d=dict(id=bid+':'+arm,arm=arm,action='NO_TRADE' if arm=='CASH' else action,ready_at=call['completed_at'],model_call_id=None if arm=='CASH' else call['id'],
               input_hash=call.get('input_hash',digest(payload)),gate_hash=digest(payload['cases'][0]['economic_gate']),
               expires_at=(instant(payload['cutoff'])+timedelta(seconds=c.get('growth_decision_validity_seconds',600))).isoformat())
        results.append(growth_execution(s,c,bid+':'+arm,d,gate,market,0 if arm=='CASH' else c['information_cost_usdt_per_call'],inputs))
    return dict(status='DATA_INSUFFICIENT' if ob['status']!='MEASURED' else 'MORE_OBSERVATION',observation=ob,economic_gate=gate,
        price_return_24h=price_return,model_call_id=call['id'],model_status=call['status'],input_hash=digest(payload),
        comparison=results,paper_trades=sum(r['status']=='FILLED' for r in results),
        pnl=None,reason='Economic proxy measured separately from unproven holder capture; fresh delayed policy actions recorded.',
        next_hypothesis='Separate accrued-interest growth from new borrowing and trace retained revenue to token demand; repeat finalized state after 24h.',
        next_due=(instant(utc())+timedelta(days=1)).isoformat())

def maintain_growth_positions(s,inputs=None):
    for entry in s.all('growth_bridge_result'):
        if entry['status']!='FILLED' or s.get('growth_bridge_exit',entry['id'],True):continue
        try:
            if inputs:inputs.check(50)
            book=fresh_book(s,entry['symbol']);due=instant(utc())>=instant(entry['exit_due'])
            stop=book['bids'][0][0]<=entry['fill']['average_price']*.98
            if not (due or stop):continue
            if inputs:inputs.check(50)
            ex,_=PublicClient(s).get(SPOT,'/api/v3/exchangeInfo',dict(symbol=entry['symbol']))
            f=fill(book,'SELL',defaults(),entry['fill']['quantity'],ex['symbols'][0]['filters'])
            s.append('growth_bridge_exit',entry['id'],dict(id=entry['id'],fill=f,reason='TIME' if due else 'STOP',
                realized_pnl_usdt=entry['fill']['cash_delta_usdt']+f['cash_delta_usdt']-entry['information_cost_usdt']))
        except Exception as exc:s.failure('growth_bridge_exit',exc,entry_id=entry['id'])


def growth_service(root):
    """Position observation has its own durable, bounded shared-authority charge."""
    with process_lock(root):
        s=Store(root);loop=None
        try:
            c=s.all('batch_config')[-1]
            if c['source_hash']!=implementation_hash():raise ValueError('SOURCE_VERSION_NOT_REGISTERED')
            pending=[r for r in s.all('growth_bridge_result') if r['status']=='FILLED' and not s.get('growth_bridge_exit',r['id'],True)]
            if not pending:return dict(status='NO_OPEN_GROWTH_POSITIONS',http_requests=0)
            alpha=Path(c['alpha_runtime']);p=readonly_rows(alpha,'alpha_protocol')[0]
            with process_lock(alpha):
                loop=open_loop(alpha,p['config'])
                interrupted=loop.db.execute("SELECT task_key FROM tasks WHERE state='CLAIMED' AND task_key LIKE 'growth-maintain-%' ORDER BY claimed_at").fetchone()
                key=interrupted['task_key'] if interrupted else 'growth-maintain-'+utc()[:16].replace(':','')
                old=s.get('growth_maintenance_receipt',key,True)
                if old:
                    accounting=ack_preserving_overrun(loop,loop.get_task(key),old,old['elapsed_seconds'])
                    if not s.get('batch_result',key,True):s.append('batch_result',key,dict(id=key,kind='POSITION_MAINTENANCE_NOT_RESEARCH',accounting=accounting))
                    return dict(result=old,accounting=accounting)
                if not loop.db.execute('SELECT 1 FROM experiments WHERE id=?',(key,)).fetchone():
                    loop.register_experiment(ExperimentSpec(key,key,'CAPITAL_GROWTH',1,utc(),(instant(utc())+timedelta(days=1)).isoformat(),0,240,'HISTORICAL'))
                    loop.record_data(key,digest(pending));loop.enqueue_task(key,key,'due_label',input_hash=digest(pending),cash_units=0,time_seconds=240,due_at=utc())
                task=loop.get_task(key)
                if task.get('overrun_receipt_json'):return dict(status='RECONCILIATION_REQUIRED',task_key=key,reservation_retained=True)
                if task['state']=='QUEUED':task=claim_owned(loop,[key],critical=True)
                if not task:return dict(status='MAINTENANCE_BUDGET_BLOCKED')
                if not s.get('research_batch',key,True):s.append('research_batch',key,dict(id=key,kind='POSITION_MAINTENANCE_NOT_RESEARCH',started_at=utc()))
                inputs=BoundedInputs(s,key,dict(c,max_batch_seconds=240,max_http_requests_per_batch=18));inputs.start_trial()
                before={r['id'] for r in s.all('http_receipt')};started=time.monotonic()
                maintain_growth_positions(s,inputs)
                elapsed=max(240 if interrupted else 0,math.ceil(time.monotonic()-started))
                result=dict(id=key,status='POSITIONS_OBSERVED',http_receipt_ids=[r['id'] for r in s.all('http_receipt') if r['id'] not in before],elapsed_seconds=elapsed,source_version=implementation_hash())
                s.append('growth_maintenance_receipt',key,result)
                accounting=ack_preserving_overrun(loop,task,result,elapsed)
                s.append('batch_result',key,dict(id=key,kind='POSITION_MAINTENANCE_NOT_RESEARCH',accounting=accounting))
                return result
        finally:
            if loop:loop.close()
            s.close()

def run(root,batch_id=None):
    root=Path(root).resolve()
    with process_lock(root):
        s=Store(root);loop=None
        try:
            c=s.all('batch_config')[-1];s.verify()
            if c['source_hash']!=implementation_hash():raise ValueError('BATCH_SOURCE_CHANGED_NEW_VERSION_REQUIRED')
            alpha=Path(c['alpha_runtime']);p=readonly_rows(alpha,'alpha_protocol')[0]
            if p['authority_id']!=c['authority_id'] or p['implementation_hash']!=source_hash():raise ValueError('BASE_IDENTITY_CHANGED')
            # Position observations run separately with their own resource receipt.
            unfinished=[b['id'] for b in s.all('research_batch') if not s.get('batch_result',b['id'],True)]
            bid=batch_id or (unfinished[0] if unfinished else 'economic-'+instant(utc()).date().isoformat())
            if not re.fullmatch(r'[A-Za-z0-9_-]+',bid):raise ValueError('BATCH_ID')
            old=s.get('batch_result',bid,True)
            if old:return dict(status='CACHED',result=old,new_independent_evidence=0)
            b=s.get('research_batch',bid,True)
            if not b:
                prior=s.all('research_batch')
                if prior and (instant(utc())-instant(prior[-1]['started_at'])).total_seconds()<c['batch_interval_hours']*3600:
                    return dict(status='NOT_DUE',next_due=(instant(prior[-1]['started_at'])+timedelta(hours=24)).isoformat())
                if (instant(utc())-instant(c['registered_at'])).days>=c['research_days']:return dict(status='RESEARCH_WINDOW_ENDED')
                b=s.append('research_batch',bid,dict(id=bid,started_at=utc(),families=list(FAMILIES),config_hash=digest(c)))
                for family,(engine,hypothesis) in FAMILIES.items():
                    s.append('hypothesis',bid+':'+family,dict(id=bid+':'+family,batch_id=bid,family=family,engine=engine,hypothesis=hypothesis,
                        version=1,registered_at=utc(),scope='HISTORICAL_EXPLORATION' if family!='credit_growth' else 'CURRENT_GROWTH_PAPER'))
            inputs=BoundedInputs(s,bid,c);results=[]
            priorities={q['family']:q['priority'] for q in s.all('next_hypothesis') if 'family' in q}
            order=sorted(FAMILIES,key=lambda f:(priorities.get(f,2),list(FAMILIES).index(f)))
            for family in order[:c['max_trials_per_batch']]:
                engine=FAMILIES[family][0]
                key=bid+':'+family;previous=s.get('trial_result',key,True)
                if previous:results.append(previous);continue
                # Shared controller, same caps, zero new alpha. The existing worker's
                # lock prevents it from trying to recover another worker's claim.
                with process_lock(alpha):
                    cases=readonly_rows(alpha,'alpha_case');outcomes={r['case_id'] for r in readonly_rows(alpha,'alpha_outcome')}
                    entries=readonly_rows(alpha,'alpha_entry_result');exits={r['id'] for r in readonly_rows(alpha,'alpha_exit')}
                    active=[e for e in entries if e['status']=='FILLED' and e['id'] not in exits]
                    imminent=any(x['id'] not in outcomes and (instant(x['due_at'])-instant(utc())).total_seconds()<c['max_trial_seconds']+60 for x in cases)
                    loop=open_loop(alpha,p['config'])
                    eid='batch-'+key
                    pending=loop.db.execute("SELECT state FROM tasks WHERE task_key=?",(key,)).fetchone()
                    recovering=pending is not None and pending['state']=='CLAIMED'
                    if (active or imminent) and not recovering:return dict(status='DEFER_TO_EXISTING_RISK_OR_MATURITY',completed=len(results))
                    if not loop.db.execute('SELECT 1 FROM experiments WHERE id=?',(eid,)).fetchone():
                        loop.register_experiment(ExperimentSpec(eid,'economic-batch/'+family+'/'+bid,engine,1,utc(),
                            (instant(utc())+timedelta(days=32)).isoformat(),0,c['max_trial_seconds'],'HISTORICAL'))
                        loop.record_data(eid,digest(b))
                        loop.enqueue_task(key,eid,'cheap_falsifier',input_hash=digest(b),cash_units=0,time_seconds=c['max_trial_seconds'],decision_value_score=1)
                    task=loop.get_task(key)
                    if task['state']=='QUEUED':
                        # Paused/foreign queues are never executed by this adapter.
                        foreign=loop.db.execute("SELECT t.* FROM tasks t JOIN experiments e ON e.id=t.experiment_id WHERE t.state IN ('QUEUED','CLAIMED') AND t.task_key!=? AND e.state IN ('DATA_READY','FORWARD_TEST')",(key,)).fetchall()
                        foreign=[t for t in foreign if t['state']=='CLAIMED' or not json.loads(t['task_json'])['due_at'] or instant(json.loads(t['task_json'])['due_at'])<=instant(utc())]
                        if foreign:
                            loop.suspend(eid,'PAUSED','Foreign controller work has priority; sidecar queue must remain ineligible.')
                            return dict(status='DEFER_TO_EXISTING_CONTROLLER_TASK',foreign_tasks=len(foreign))
                        if loop.db.execute('SELECT state FROM experiments WHERE id=?',(eid,)).fetchone()[0]=='PAUSED':loop.resume(eid)
                        task=claim_owned(loop,[key])
                        if task is None:
                            loop.suspend(eid,'PAUSED','Insufficient unchanged controller budget; prevent another worker claiming this adapter task.')
                            return dict(status='BUDGET_BLOCKED',completed=len(results))
                    checkpoint=s.get('trial_receipt',key,True)
                    if task.get('overrun_receipt_json'):
                        return dict(status='RECONCILIATION_REQUIRED',task_key=key,reservation_retained=True,alpha_maintenance_must_continue=True)
                    if checkpoint:result=checkpoint['result'];elapsed=checkpoint['elapsed_seconds']
                    else:
                        started=time.monotonic()
                        inputs.start_trial()
                        try:
                            if recovering and (active or imminent):raise ValueError('INTERRUPTED_WORK_YIELDS_TO_EXISTING_DUE_RISK')
                            inputs.check();result=globals()[family](s,c,bid,inputs)
                        except Exception as exc:result=dict(status='EXECUTION_ERROR',error=type(exc).__name__+':'+str(exc),paper_trades=0,
                            next_hypothesis='Repair the recorded failure without silently retrying charged calls.',next_due=(instant(utc())+timedelta(days=1)).isoformat())
                        elapsed=math.ceil(time.monotonic()-started)
                        if recovering:elapsed=max(elapsed,c['max_trial_seconds'])
                        checkpoint=s.append('trial_receipt',key,dict(id=key,result=result,input_hash=digest(b),output_hash=digest(result),elapsed_seconds=elapsed,
                            time_accounting='CONSERVATIVE_FULL_RESERVATION_AFTER_INTERRUPTION' if recovering else 'MEASURED_WALL_SECONDS',actual_incremental_cash_charge=0))
                    accounting=ack_preserving_overrun(loop,task,result,elapsed,result['status']!='EXECUTION_ERROR')
                    if accounting['status']=='RECONCILIATION_REQUIRED':
                        s.append('trial_accounting',key,dict(id=key,**accounting,reported_seconds=elapsed,at=utc()))
                        return dict(**accounting,alpha_maintenance_must_continue=True)
                    row=s.append('trial_result',key,dict(id=key,batch_id=bid,family=family,completed_at=utc(),**result))
                    s.append('next_hypothesis',key,dict(id=key,parent_trial=key,family=family,change=result['next_hypothesis'],due_at=result['next_due'],
                        priority=0 if result['status']=='EXECUTION_ERROR' else 1 if result['status']=='DATA_INSUFFICIENT' else 2,
                        selection_reason='Integrity repair, missing causal evidence, then independent falsification; no retrospective tuning.'))
                    results.append(row);loop.close();loop=None
            a=sqlite3.connect(Path(json.loads((alpha/'authority.json').read_text())['database_path']))
            try:allocation=a.execute('SELECT j,alpha FROM alpha_registry ORDER BY j DESC LIMIT 1').fetchone()
            finally:a.close()
            diagnostic=design_diagnostic(p['config'],allocation[1])
            s.append('design_diagnostic',bid,dict(id=bid,**diagnostic))
            counts={status:sum(r['status']==status for r in results) for status in ('REJECTED','NEXT_VALIDATION','MORE_OBSERVATION','DATA_INSUFFICIENT','EXECUTION_ERROR')}
            return s.append('batch_result',bid,dict(id=bid,completed_at=utc(),attempted=len(results),completed=len(results)-counts['EXECUTION_ERROR'],counts=counts,
                paper_entries=sum(r['paper_trades'] for r in results),new_independent_matured_forward_results=0,
                prior_alpha_pending=len([x for x in readonly_rows(alpha,'alpha_case') if x['id'] not in {o['case_id'] for o in readonly_rows(alpha,'alpha_outcome')}]),
                next_queue=[q['id'] for q in sorted(s.all('next_hypothesis'),key=lambda q:(q['priority'],q['id']))],
                model_calls=len([x for x in s.all('batch_model_intent') if x['batch_id']==bid]),design_diagnostic_id=bid,
                edge_proven=False,live_orders=0))
        finally:
            if loop:loop.close()
            s.close()

def recover(root):
    s=Store(root)
    try:
        c=s.all('batch_config')[-1];auth=json.loads((Path(c['alpha_runtime'])/'authority.json').read_text())
        db=sqlite3.connect(auth['database_path']);db.row_factory=sqlite3.Row
        try:tasks=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE state='CLAIMED' ORDER BY claimed_at")]
        finally:db.close()
        owned={r['id'] for r in s.all('hypothesis_spec')}
        blocked=[r['task_key'] for r in tasks if r.get('overrun_receipt_json')]
        pending=[r for r in tasks if not r.get('overrun_receipt_json') and r['experiment_id'].startswith('batch-')]
        child=any(r['task_key'].startswith('child:') and r['task_key'][6:] in owned and not r.get('overrun_receipt_json') for r in tasks)
    finally:s.close()
    results=[run(root,r['task_key'].split(':')[0]) for r in pending]
    if child:
        from .hypothesis_loop import run_queue
        results.append(run_queue(root))
    return dict(status='RECONCILIATION_REQUIRED' if blocked else 'RECOVERED' if results else 'NO_RECOVERY_NEEDED',unresolved_claims=blocked,reservation_retained=bool(blocked),results=results)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime',type=Path,required=True);p.add_argument('--alpha-runtime',type=Path)
    p.add_argument('--batch-id');p.add_argument('--request-id');p.add_argument('--max-items',type=int,default=2);p.add_argument('--revision-reason')
    p.add_argument('command',choices=['init','run','recover','revise-implementation','alpha-service','growth-service','run-queue','accounts'])
    a=p.parse_args()
    if a.command=='init' and not a.alpha_runtime:p.error('--alpha-runtime required for init')
    if a.command=='recover':
        result=recover(a.runtime)
    elif a.command=='run-queue':
        from .hypothesis_loop import run_queue
        result=run_queue(a.runtime,a.request_id,a.max_items)
    elif a.command=='alpha-service':
        from .batch_control import alpha_service
        s=Store(a.runtime)
        try:
            c=s.all('batch_config')[-1]
            if c['source_hash']!=implementation_hash():raise ValueError('SOURCE_VERSION_NOT_REGISTERED')
        finally:s.close()
        result=alpha_service(Path(c['alpha_runtime']))
    elif a.command=='growth-service':result=growth_service(a.runtime)
    elif a.command=='accounts':
        s=Store(a.runtime)
        try:result=policy_costs(s)
        finally:s.close()
    elif a.command=='revise-implementation':result=revise_implementation(a.runtime,a.revision_reason)
    else:result=initialize(a.runtime,a.alpha_runtime) if a.command=='init' else run(a.runtime,a.batch_id)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
