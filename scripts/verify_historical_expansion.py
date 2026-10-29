"""Standalone raw-byte recalculation. Standard library; no ONE code imports."""
import argparse,csv,hashlib,io,json,math,statistics,zipfile
from pathlib import Path
from datetime import datetime,timezone
H=3600000
ABS='SPOT_SELL_PRESSURE_ABSORPTION';DELAY='CROSS_ASSET_INFORMATION_DELAY'
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT'];ARMS=['ECONOMIC_SELECTION','PRICE_SELECTOR','BTC','CASH']
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def same(a,b,path='root'):
    if type(a)==float:
        assert type(b) in (float,int) and math.isfinite(b) and math.isclose(a,b,abs_tol=1e-8,rel_tol=1e-11),(path,a,b)
    elif isinstance(a,dict):
        assert a.keys()==b.keys(),(path,a.keys(),b.keys())
        for k in a:same(a[k],b[k],path+'.'+str(k))
    elif isinstance(a,list):
        assert len(a)==len(b),(path,len(a),len(b))
        for i,(x,y) in enumerate(zip(a,b)):same(x,y,path+'.'+str(i))
    else:assert type(a)==type(b) and a==b,(path,a,b)
def epoch(x):return int(datetime.fromisoformat(x.replace('Z','+00:00')).timestamp()*1000)
def choose(a,m):
    if m==ABS:eligible=[s for s in SYMS if a[s]['past_24h_return']<0 and a[s]['aggressive_sell_share']>=.6 and a[s]['volume_ratio']>=1.5 and abs(a[s]['hour_return'])<=.002]
    elif m==DELAY:
        eligible=[s for s in ['ETHUSDT','SOLUSDT'] if a[s]['past_24h_return']-a['BTCUSDT']['past_24h_return']<=-.01] if a['BTCUSDT']['past_24h_return']>=.02 and a['BTCUSDT']['volume_ratio']>=1.5 else []
    else:eligible=[s for s in SYMS if a[s]['past_24h_return']<0]
    return min(eligible,key=lambda s:(a[s]['past_24h_return'],s)) if eligible else None
def verify(root):
    records=[json.loads(x) for x in (root/'ledgers/batch.jsonl').read_text('utf8').splitlines()]
    bodies=lambda kind:[r['body'] for r in records if r['kind']==kind]
    result=bodies('expansion_result')[-1];spec=next(s for s in bodies('expansion_spec') if s['id']==result['id']);assert result['spec_hash']==digest(spec)
    original=next(s for s in bodies('expansion_spec') if s['id']==spec['reuse_operation_id']);assert spec['parent_spec_hash']==digest(original)
    manifest=next(r['manifest'] for r in bodies('expansion_manifest') if r['id']==spec['id']);assert digest(manifest)==spec['manifest_hash']
    assert original['rules']==spec['rules'] and original['costs']==spec['costs'] and original['blocks']==spec['blocks']
    assert spec['entry_delay_hours']==2 and spec['horizons']==[4,24] and spec['lockout_hours']==26
    receipts={r['id']:r for r in bodies('http_receipt')};allattempts=[r for r in bodies('extension_http_intent') if r['operation_id']==original['id']]
    assert len(allattempts)<=90
    for intent in allattempts:
        r=receipts[intent['id']];assert epoch(original['registered_at'])<=epoch(intent['at'])<=epoch(r['received_at'])<=epoch(spec['registered_at'])
        assert r['operation_id']==original['id'] and r['url']==intent['url']
    assert not any(r['operation_id']==spec['id'] for r in bodies('extension_http_intent'))
    data={s:{'bars':{},'funding':{}} for s in SYMS};quarantine=[]
    for binding in result['bindings']:
        r=binding['receipt'];raw=(root/'raw/batch'/(r['sha256']+'.bin')).read_bytes();assert hashlib.sha256(raw).hexdigest()==r['sha256'];assert len(raw)==r['bytes']
        if raw[:2]==b'PK':
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                name,=[n for n in z.namelist() if n.endswith('.csv')];rows=[{'fundingTime':int(float(r['calc_time'])),'fundingRate':r['last_funding_rate']} for r in csv.DictReader(io.StringIO(z.read(name).decode()))]
        else:rows=json.loads(raw)
        kind=binding['kind'];dest=data[binding['symbol']][kind]
        for row in rows:
            t=row[0] if kind=='bars' else row['fundingTime']
            if kind=='bars':
                assert row[6]<epoch(r['received_at'])
                if row[6]!=t+H-1:
                    assert t<=row[6]<t+H-1
                    quarantine.append(dict(symbol=binding['symbol'],open_ms=t,close_ms=row[6],receipt_hash=r['sha256'],reason='SHORT_TERMINAL_BAR_EXCLUDED_NOT_EXTENDED'));continue
                assert all(math.isfinite(float(row[i])) and float(row[i])>=0 for i in (1,2,3,4,5,7,9,10));assert 0<=float(row[10])<=float(row[7])+1e-7
            else:row={'fundingTime':t,'fundingRate':str(row['fundingRate'])}
            if t in dest:assert dest[t]==row or (kind=='funding' and float(dest[t]['fundingRate'])==float(row['fundingRate']))
            dest[t]=row
    same(quarantine,result['quarantined_bars'])
    times=sorted(set.intersection(*[set(data[s]['bars']) for s in SYMS]));cs=[]
    ranges=[spec['historical_range'],spec['reused_range']]
    for t in times:
        if not any(start<=t<end for start,end in ranges):continue
        a={}
        for symbol in SYMS:
            b=data[symbol]['bars'];required=[t+k*H for k in list(range(-25,0))+list(range(2,27))]
            if not all(v in b for v in required):break
            funds=[data[symbol]['funding'][v] for v in sorted(data[symbol]['funding']) if v<=t]
            if len(funds)<3 or t-funds[-1]['fundingTime']>12*H:break
            last=b[t-H];q=float(last[7]);mean=sum(float(b[t-k*H][7]) for k in range(2,26))/24
            if min(q,mean)<=0:break
            a[symbol]=dict(past_24h_return=float(last[4])/float(b[t-24*H][1])-1,aggressive_sell_share=1-float(last[10])/q,volume_ratio=q/mean,hour_return=float(last[4])/float(last[1])-1,last_feature_close_ms=last[6],quote_volume=q,taker_buy_quote=float(last[10]),latest_funding=float(funds[-1]['fundingRate']),last_three_funding_sum=sum(float(f['fundingRate']) for f in funds[-3:]),funding_source_time=funds[-1]['fundingTime'])
        if len(a)==3:cs.append((t,a))
    block=lambda t:next(b['id'] for b in spec['blocks'] if b['start_ms']<=t<b['end_ms'])
    labels={};cluster_block={};boundary=-1;lineage=digest(result['bindings'])
    for t,a in cs:
        if choose(a,ABS) or choose(a,DELAY):
            if t>=boundary:start=t
            labels[t]='shared-'+digest([lineage,start])[:16];cluster_block[labels[t]]=block(start);boundary=t+26*H
    def path(symbol,t,h):
        entry=float(data[symbol]['bars'][t+2*H][1]) if symbol else None;exit=float(data[symbol]['bars'][t+(2+h)*H][1]) if symbol else None
        qty=1250/(entry*1.0016) if symbol else 0.;gross=qty*(exit-entry) if symbol else 0.;turnover=qty*(entry+exit) if symbol else 0.
        fee=turnover*.001;slippage=turnover*.0005;spread=turnover*.0001;net=qty*exit*(1-.0016)-1250 if symbol else 0.
        return dict(symbol=symbol,action='LONG' if symbol else 'NO_TRADE',entry_ms=t+2*H,exit_ms=t+(2+h)*H,entry_price=entry,exit_price=exit,quantity=qty,gross_usdt=gross,fee_usdt=fee,slippage_usdt=slippage,spread_usdt=spread,cost_usdt=fee+slippage+spread,net_usdt=net,turnover_usdt=turnover,exposure_hours=h if symbol else 0,capital_occupied_hours=26 if symbol else 0,trades=int(bool(symbol)),information_cost_usdt=0,funding_usdt=0,not_a_fill=True)
    events=[];signals=[];full=[];busy={m:-1 for m in (ABS,DELAY,'PRICE')}
    for t,a in cs:
        ps=choose(a,'PRICE')
        if ps and t>=busy['PRICE']:
            busy['PRICE']=t+26*H;full.append(dict(signal_ms=t,block=block(t),paths={str(h):path(ps,t,h) for h in (4,24)}))
        for m in (ABS,DELAY):
            symbol=choose(a,m)
            if symbol is None:continue
            accepted=t>=busy[m];signals.append(dict(mechanism=m,signal_ms=t,symbol=symbol,accepted=accepted,cluster_id=labels[t]))
            if not accepted:continue
            busy[m]=t+26*H
            events.append(dict(mechanism=m,signal_ms=t,block=block(t),cluster_block=cluster_block[labels[t]],cluster_id=labels[t],features=a,arms={arm:{str(h):path(selected,t,h) for h in (4,24)} for arm,selected in zip(ARMS,[symbol,ps,'BTCUSDT',None])}))
    same(events,result['events'],'events');same(signals,result['signals'],'signals');same(full,result['full_price_policy'],'full_price')
    metrics=['gross_usdt','fee_usdt','slippage_usdt','spread_usdt','cost_usdt','net_usdt','turnover_usdt','exposure_hours','capital_occupied_hours','trades','information_cost_usdt','funding_usdt']
    total=lambda xs:{k:sum(x[k] for x in xs) for k in metrics}
    summaries={}
    for bid in ['ALL']+[b['id'] for b in spec['blocks']]:
        ev=[e for e in events if bid=='ALL' or e['block']==bid]
        summaries[bid]={m:{arm:{str(h):total([e['arms'][arm][str(h)] for e in ev if e['mechanism']==m]) for h in (4,24)} for arm in ARMS} for m in (ABS,DELAY)}
        summaries[bid]['PRICE_ONLY']={str(h):total([e['paths'][str(h)] for e in full if bid=='ALL' or e['block']==bid]) for h in (4,24)}
    same(summaries,result['summaries'],'summaries')
    scores=[]
    for cid in dict.fromkeys(e['cluster_id'] for e in events):
        ev=[e for e in events if e['cluster_id']==cid];mechanisms=sorted(set(e['mechanism'] for e in ev));values={}
        for h in ('4','24'):
            nets={arm:statistics.mean(statistics.mean(e['arms'][arm][h]['net_usdt'] for e in ev if e['mechanism']==m) for m in mechanisms) for arm in ARMS}
            values[h]=dict(economic_net=nets['ECONOMIC_SELECTION'],selection_increment=nets['ECONOMIC_SELECTION']-nets['PRICE_SELECTOR'],btc_difference=nets['ECONOMIC_SELECTION']-nets['BTC'])
        scores.append(dict(id=cid,block=cluster_block[cid],mechanisms=mechanisms,events=len(ev),scores=values))
    same(scores,result['cluster_scores'],'cluster_scores')
    means={h:{axis:statistics.mean(r['scores'][h][axis] for r in scores) if scores else None for axis in ('economic_net','selection_increment','btc_difference')} for h in ('4','24')}
    same(means,result['cluster_means'],'means')
    classify=lambda axes:'INCONCLUSIVE' if len(scores)<10 else 'REJECT' if any(x<=0 for x in axes) else 'CANDIDATE_FOR_FRESH_VALIDATION'
    expected=dict(unchanged_24h=classify([means['24']['economic_net']]),short_4h=classify([means['4']['economic_net'],means['4']['selection_increment']]),selection_4h=classify([means['4']['selection_increment']]),selection_24h=classify([means['24']['selection_increment']]),timing='TIMING_CONTRIBUTION_UNRESOLVED')
    same(expected,result['verdicts'],'verdicts')
    same({m:{h:summaries['ALL'][m]['ECONOMIC_SELECTION'][h]['net_usdt']-summaries['ALL']['PRICE_ONLY'][h]['net_usdt'] for h in ('4','24')} for m in (ABS,DELAY)},result['whole_policy_net_gap'],'whole_gap')
    coverage={b['id']:dict(expected=(b['end_ms']-b['start_ms'])//H,complete=sum(b['start_ms']<=t<b['end_ms'] for t,a in cs)) for b in spec['blocks']};same(coverage,result['period_coverage'])
    same(dict(expected_clocks=sum((b-a)//H for a,b in ranges),complete_clocks=len(cs),missing_clocks=sum((b-a)//H for a,b in ranges)-len(cs),accepted_economic_events=len(events),temporal_clusters=len(scores),price_policy_trades=len(full),new_forward_events=0,paper_fills=0,model_calls=0),result['counts'])
    operations=bodies('extension_operation');done=next(r for r in operations if r['id']==spec['id']);assert done['result']==result and done['success']
    failed=next(r for r in operations if r['id']==original['id']);assert not failed['success'] and digest(failed)==spec['parent_failure_hash']
    authority=json.loads((root/'authority.json').read_bytes());task=next(t for t in authority['tasks'] if t['task_key']==done['id']);assert task['state']=='DONE' and task['actual_time']==done['elapsed_seconds']
    assert json.loads(task['result_json'])==done
    return dict(status='PASS_RAW_RECOMPUTATION',counts=result['counts'],raw_requests=len(allattempts),quarantined_bars=len(quarantine),policy_paths=len(events)*8+len(full)*2,cluster_means=means,verdicts=expected,period_coverage=coverage,independent_forward_evidence=0)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('latest',type=Path);a=p.parse_args();print(json.dumps(verify(a.latest),indent=2))
