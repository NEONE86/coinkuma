"""Standalone stdlib audit. Does not import the collector, strategy or fill engine.

Usage: python one_trading/alpha_loop/audit.py /private/runtime
Local hashes prove internal consistency, not external timestamp notarization.
"""
import hashlib,json,math,sqlite3,sys
from pathlib import Path
from datetime import datetime,timedelta
from decimal import Decimal

def sha(v):return hashlib.sha256(v).hexdigest()
def digest(v):return sha(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())
def at(v):
    d=datetime.fromisoformat(v.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('AUDIT_NAIVE_CLOCK')
    return d
def require(ok,label):
    if not ok:raise ValueError('AUDIT:'+label)
def equal(a,b,label):require(math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-10),label)

def audit_runtime(root):
    root=Path(root);db=sqlite3.connect(root/'research.sqlite3');records={};count=0
    try:
        for kind,key,body,h in db.execute('SELECT kind,id,body,sha256 FROM records ORDER BY sequence'):
            require(sha(body.encode())==h,'ENVELOPE_HASH');records.setdefault(kind,{})[key]=json.loads(body);count+=1
    finally:db.close()
    def rows(kind):return list(records.get(kind,{}).values())
    def get(kind,key):return records[kind][key]
    def raw(receipt):
        p=root/'raw'/(receipt['sha256']+'.bin');b=p.read_bytes();require(sha(b)==receipt['sha256'],'RAW_HASH');return json.loads(b)
    for r in rows('http_receipt')+rows('flow_frame'):
        p=root/'raw'/(r['sha256']+'.bin');require(sha(p.read_bytes())==r['sha256'],'ALL_RAW_HASHES')
    if not rows('alpha_protocol'):return dict(passed=True,records=count,cases=0,outcomes=0,scope='NO_PROTOCOL')
    protocol=get('alpha_protocol','frozen');c=protocol['config'];require(digest(c)==protocol['config_hash'],'CONFIG')
    def book(rid):
        receipt=get('http_receipt',rid);data=raw(receipt)
        require('/api/v3/depth?' in receipt['url'] and receipt['error'] is None,'BOOK_SOURCE')
        bids=[[float(p),float(q)] for p,q in data['bids']];asks=[[float(p),float(q)] for p,q in data['asks']]
        require(bids and asks and bids==sorted(bids,reverse=True) and asks==sorted(asks) and bids[0][0]<asks[0][0],'BOOK_ORDER')
        return dict(bids=bids,asks=asks,mid=(bids[0][0]+asks[0][0])/2,receipt=receipt)
    def state(fits,books,symbol,when):
        f=fits[symbol];others=f['benchmark_symbols'];hours=(at(when).timestamp()*1000-f['fit_end_ms'])/3600000
        require(hours>=0,'STATE_BEFORE_FIT')
        benchmark=sum(math.log(books[z]['mid']/fits[z]['anchor_price']) for z in others)/len(others)
        ref=f['anchor_price']*math.exp(f['beta']*benchmark+f['alpha_hourly']*hours)
        residual=math.log(books[symbol]['mid']/ref);sigma=f['sigma_hourly']*math.sqrt(max(hours,1/60))
        return dict(reference_price=ref,residual=residual,sigma=sigma,z=residual/sigma,mid=books[symbol]['mid'])
    for w in rows('flow_window'):
        require(at(w['started_at'])<=at(w['completed_at']),'FLOW_WINDOW_CLOCK')
        for e in w['events']:
            r=get('flow_frame',e['frame_id']);d=raw(r);x=d['data'][e['item_index']]
            require(d['topic']=='allLiquidation.'+e['symbol'] and x['s']==e['symbol'],'FLOW_ASSET')
            require(e['raw_hash']==r['sha256'] and e['received_at']==r['received_at'],'FLOW_RECEIPT')
            require(e['forced_side']==('SELL' if x['S']=='Buy' else 'BUY'),'POSITION_SIDE_INVERSION')
            equal(e['quantity_base'],float(x['v']),'FLOW_UNITS');equal(e['bankruptcy_price'],float(x['p']),'FLOW_PRICE')
            equal(at(e['event_at']).timestamp()*1000,float(x['T']),'FLOW_TIME')
            require(-3<=(at(e['received_at'])-at(e['event_at'])).total_seconds()<=c['max_event_delay_seconds'],'FLOW_DELAY')
        if w['coverage']=='CONTIGUOUS_SUBSCRIBED_WINDOW':
            require(w['error'] is None and w['subscription_acknowledged'] and w['duplicate_frames']==0 and
                    w['elapsed_seconds']>=c['stream_seconds'] and w['last_pong_at'] and
                    (at(w['completed_at'])-at(w['last_pong_at'])).total_seconds()<40,'FLOW_COVERAGE')
    for s in rows('alpha_snapshot'):
        require(at(get('flow_window',s['flow_window_id'])['completed_at'])<=at(s['cutoff']),'FLOW_AFTER_CUTOFF')
        series={}
        for symbol,rid in s['fit_receipts'].items():
            r=get('http_receipt',rid);data=raw(r);series[symbol]=data
            require(at(r['received_at'])<=at(s['cutoff']),'FIT_FUTURE_RECEIPT')
            require([x[0] for x in data]==list(range(s['fit_end_ms']-(c['fit_hours']+1)*3600000,s['fit_end_ms'],3600000)),'FIT_TIMES')
            require(all(x[6]==x[0]+3599999 and x[6]<s['fit_end_ms'] for x in data),'FIT_OPEN_BAR')
        if s['fits']:
            returns={symbol:[math.log(float(b[4])/float(a[4])) for a,b in zip(v,v[1:])] for symbol,v in series.items()}
            for symbol,f in s['fits'].items():
                other=[z for z in s['universe'] if z!=symbol];n=c['fit_hours']
                x=[sum(returns[z][i] for z in other)/len(other) for i in range(n)];y=returns[symbol]
                mx=sum(x)/n;my=sum(y)/n
                beta=sum((a-mx)*(b-my) for a,b in zip(x,y))/sum((a-mx)**2 for a in x)
                alpha=my-beta*mx;sigma=math.sqrt(sum((b-alpha-beta*a)**2 for a,b in zip(x,y))/(n-2))
                for field,value in [('beta',beta),('alpha_hourly',alpha),('sigma_hourly',sigma),('anchor_price',float(series[symbol][-1][4]))]:equal(f[field],value,'OLS_'+field)
        for symbol,b in s['books'].items():
            observed=book(b['receipt_id']);equal(b['mid'],observed['mid'],'SNAPSHOT_MID')
            require(at(observed['receipt']['received_at'])<=at(s['cutoff']),'BOOK_AFTER_CUTOFF')
        if s['fits']:
            bb={z:book(b['receipt_id']) for z,b in s['books'].items()}
            for symbol,v in s['state'].items():
                for field,value in state(s['fits'],bb,symbol,s['cutoff']).items():equal(v[field],value,'STATE_'+field)
    previous=None
    for case in rows('alpha_case'):
        s=get('alpha_snapshot',case['snapshot_id'])
        require(case['cutoff']==s['cutoff'] and at(protocol['registered_at'])<at(case['registered_at'])<at(case['starts_at'])<at(case['due_at']),'CASE_CHRONOLOGY')
        require(at(case['cutoff'])<=at(case['registered_at']),'PREREG_INPUT')
        require((at(case['due_at'])-at(case['starts_at'])).total_seconds()==c['horizon_seconds'],'COMMON_HORIZON')
        require(previous is None or at(case['starts_at'])>=at(previous),'OVERLAPPING_CASE')
        previous=case['due_at']
    for call in rows('model_call'):
        folder=root/call['artifact_directory'];payload=json.loads((folder/'input.json').read_text(encoding='utf-8'))
        require(digest(payload)==call['input_hash'],'MODEL_INPUT_HASH')
        require(sha((folder/'prompt.txt').read_bytes())==call['prompt_hash'],'MODEL_PROMPT_HASH')
        require(at(payload['snapshot']['cutoff'])<=at(call['started_at'])<=at(call['completed_at']),'MODEL_CLOCK')
        require(payload['snapshot']==get('alpha_snapshot',payload['snapshot']['id']),'MODEL_SNAPSHOT')
        if call['status']=='CONNECTED_VALID':
            a=json.loads((folder/'answer.json').read_text(encoding='utf-8'))
            require(a==call['answer'] and digest(a)==call['response_hash'],'MODEL_RESPONSE')
        for m in payload['memory']:
            require(m['origin']=='FORWARD_PAPER' and m['independent_audit_passed'] and all(at(m[k])<=at(payload['snapshot']['cutoff']) for k in ['due_at','received_at','stored_at']),'FUTURE_MEMORY')
    for d in rows('alpha_decision'):
        case=get('alpha_case',d['case_id']);require(at(d['ready_at'])>=at(case['cutoff']),'DECISION_CLOCK')
        if d['status']=='DECIDED' and d['arm'] in ('CF_SEMANTIC','CF_SEMANTIC_MEMORY'):
            call=get('model_call',d['model_call_ids'][-1]);require(call['status']=='CONNECTED_VALID' and d['answer']==call['answer'],'DECISION_RESPONSE')
            require(d['input_hash']==call['input_hash'] and d['ready_at']==call['completed_at'],'DECISION_BINDING')
            require(d['action']==call['answer']['action'] or (d['action']=='NO_TRADE' and d['gate_reason']),'ACTION_MUTATION')
        if d['arm']=='CF_PRICE_ONLY':
            s=get('alpha_snapshot',case['snapshot_id']);eligible=[(abs(v['z']),symbol) for symbol,v in s['state'].items() if v['z']<=-c['entry_abs_z']]
            expected=max(eligible,default=(0,'CASH'))[1];require(d['symbol']==expected,'BASELINE_FULL_UNIVERSE')
    fills=0
    for kind in ('alpha_entry_result','alpha_exit'):
        for e in rows(kind):
            if not e.get('fill'):continue
            fills+=1;f=e['fill'];case=get('alpha_case',e['case_id']);decision=get('alpha_decision',e['case_id']+':'+e['arm']);b=book(f['quote_receipt_id'])
            ready=at(decision['ready_at'])+timedelta(seconds=c['execution_delay_seconds'])
            require(at(b['receipt']['requested_at'])>=ready and f['quote_received_at']==b['receipt']['received_at'],'STALE_OR_PREDECISION_FILL')
            require(at(f['simulated_at'])>=at(f['quote_received_at']),'FILL_CLOCK')
            qty=f['quantity'];remaining=qty;gross=0.;unadjusted=0.;side=f['side'];levels=b['asks'] if side=='BUY' else b['bids'];best=levels[0][0]
            filters=get('alpha_snapshot',case['snapshot_id'])['filters'][e['symbol']];lot=next(x for x in filters if x['filterType']=='LOT_SIZE')
            require(Decimal(str(qty))%Decimal(lot['stepSize'])==0,'LOT_STEP')
            for price,size in levels:
                if abs(price/best-1)*10000>c['max_book_impact_bps']:break
                take=min(remaining,size*c['book_participation']);unadjusted+=take*price
                gross+=take*price*(1+(1 if side=='BUY' else -1)*c['slippage_bps_per_side']/10000);remaining-=take
                if remaining<1e-10:break
            require(remaining<1e-10,'DEPTH_CAPACITY');equal(f['gross_usdt'],gross,'EXECUTION_PRICE')
            equal(f['fee_usdt'],gross*c['fee_bps_per_side']/10000,'FEE')
            equal(f['slippage_cost_usdt'],abs(gross-unadjusted),'SLIPPAGE')
            equal(f['spread_depth_cost_usdt'],(unadjusted-qty*b['mid']) if side=='BUY' else (qty*b['mid']-unadjusted),'SPREAD_DEPTH')
            equal(f['cash_delta_usdt'],-gross-f['fee_usdt'] if side=='BUY' else gross-f['fee_usdt'],'CASH_DELTA')
            require(f['funding_status']=='NOT_APPLICABLE_SPOT' and f['funding_cost_usdt']==0,'FUNDING')
            snapshot=get('alpha_snapshot',case['snapshot_id'])
            observed={s:book(rid) for s,rid in e['observation_receipts'].items()}
            calculated=state(snapshot['fits'],observed,e['symbol'],e['state_evaluated_at'])
            for field,value in calculated.items():equal(e['state'][field],value,'EXECUTION_STATE_'+field)
            if kind=='alpha_entry_result':
                gap=calculated['reference_price']/b['asks'][0][0]-1
                cost=2*(c['fee_bps_per_side']+c['slippage_bps_per_side'])/10000+(b['asks'][0][0]-b['bids'][0][0])/b['mid']
                equal(e['remaining_gap'],gap,'GAP');equal(e['estimated_roundtrip_cost'],cost,'ESTIMATED_COST')
                require(gap>cost,'GAP_COST_GATE')
            else:
                entry=get('alpha_entry_result',e['id']);equal(qty,entry['fill']['quantity'],'EXIT_QUANTITY')
                equal(e['realized_pnl_usdt'],entry['fill']['cash_delta_usdt']+f['cash_delta_usdt'],'REALIZED_PNL')
                if e['reason']=='COMMON_HORIZON':require(at(f['quote_received_at'])>=at(case['due_at']),'EARLY_HORIZON_EXIT')
                elif e['reason']=='ADVERSE_CONTINUATION_STOP':require(b['bids'][0][0]<=entry['fill']['average_price']*(1-c['stop_fraction']),'FALSE_STOP')
                elif e['reason']=='RESIDUAL_NORMALIZATION':require(calculated['z']>=-c['normalization_abs_z'],'FALSE_NORMALIZATION')
                else:require(False,'UNREGISTERED_EXIT_REASON')
    for o in rows('alpha_outcome'):
        case=get('alpha_case',o['case_id']);require(at(o['received_at'])>=at(case['due_at']),'IMMATURE_OUTCOME')
        for track in ['NATIVE','MATCHED']:
            for arm,value in o[track.lower()].items():
                if value['metrics'] is None:continue
                d=get('alpha_decision',case['id']+':'+arm);require(d['status']=='DECIDED','FAILED_MODEL_AS_CASH')
                entry=get('alpha_entry_result',case['id']+':'+arm+':'+track)
                info=len(d['model_call_ids'])*c['information_cost_usdt_per_model_call']/c['capital_usdt']
                if entry['status']=='FILLED':
                    exit=get('alpha_exit',entry['id']);a=entry['fill'];b=exit['fill']
                    gross=a['quantity']*(b['mid']-a['mid'])/c['capital_usdt']
                    cost=sum(x[k] for x in (a,b) for k in ['fee_usdt','spread_depth_cost_usdt','slippage_cost_usdt'])/c['capital_usdt']
                else:gross=cost=0.
                for k,v in [('gross_return',gross),('trading_cost',cost),('information_cost',info)]:equal(value['metrics'][k],v,'OUTCOME_'+k)
                equal(value['net_return'],gross-cost-info,'NET_RETURN')
        initial=get('flow_window',case['flow_window_id']);final=get('flow_window',o['final_flow_window_id'])
        for symbol,sides in o['economic'].items():
            for side,label in sides.items():
                totals=[sum(e['quantity_base'] for e in w['events'] if e['symbol']==symbol and e['forced_side']==side) for w in (initial,final)]
                expected='UNKNOWN'
                if totals[0]>0 and all(w['coverage']=='CONTIGUOUS_SUBSCRIBED_WINDOW' for w in (initial,final)):
                    expected='TRUE' if totals[1]<=c['flow_ratio_threshold']*totals[0] else 'FALSE'
                require(label==expected,'ECONOMIC_NOT_PRICE_LABEL')
    return dict(passed=True,records=count,cases=len(rows('alpha_case')),outcomes=len(rows('alpha_outcome')),
                actual_model_calls=len(rows('model_call')),virtual_fills=fills,
                evidence_origin='ACTUAL_RECEIPTS_LOCAL_HASHES_NOT_EXTERNAL_ATTESTATION',edge_proven=False,real_orders=0)

if __name__=='__main__':print(json.dumps(audit_runtime(sys.argv[1]),indent=2))
