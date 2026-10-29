"""Fixed historical expansion: same economic gates, matched selection controls.

No claim of untouched data, event timing identification, or executable old quotes.
"""
import argparse,csv,hashlib,io,json,math,statistics,zipfile
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .autonomy.store import Store,utc,instant,digest,process_lock,atomic_bytes
from .autonomy.market import SPOT,FUTURES
from .hypothesis_loop import ABSORPTION,DELAY,select,RULES
from .event_scan import contexts,clusters
from .expansion_io import owned,Inputs,version
H=3600000
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
ARMS=('ECONOMIC_SELECTION','PRICE_SELECTOR','BTC','CASH')
COSTS=dict(capital_usdt=10000,exposure_fraction=.125,fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1)
def ms(s):return int(instant(s).timestamp()*1000)
def iso(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def price(c):
    symbol=min(c['assets'],key=lambda s:(c['assets'][s]['past_24h_return'],s))
    return symbol if c['assets'][symbol]['past_24h_return']<0 else None

def register(s,manifest):
    key='expansion-2021-2022-v1';old=s.get('expansion_spec',key,True)
    if old:
        if old['manifest_hash']!=digest(manifest):raise ValueError('MANIFEST_IDENTITY')
        return old
    start=ms('2021-01-01T00:00:00+00:00');end=ms('2023-01-01T00:00:00+00:00')
    # Full years in chronological order; a third year requires >90 attempts
    # including 9 reserved retries. Unfetched remainder is explicitly blocked.
    requests=[]
    cached_funding={s:[] for s in SYMBOLS}
    for r in manifest['items']:
        if r['symbol'] in SYMBOLS and '/fundingRate/' in r['source'] and r['source'].endswith('.zip') and r['first_ms'] and r['last_ms']>=start-48*H and r['first_ms']<=end:
            cached_funding[r['symbol']].append(r)
    for symbol in SYMBOLS:
        for a in range(start-25*H,end+26*H,1000*H):requests.append(dict(symbol=symbol,kind='bars',start=a,end=min(a+1000*H,end+26*H)-1))
        # Funding APIs fill exact required coverage even if the imported archive
        # does not cover the entire fixed range. Cached months are still retained.
        covered=cached_funding[symbol]
        if not covered or min(r['first_ms'] for r in covered)>start-24*H or max(r['last_ms'] for r in covered)<end-12*H:
            for a in range(start-48*H,end,8000*H):requests.append(dict(symbol=symbol,kind='funding',start=a,end=min(a+8000*H,end)-1))
    blocks=[dict(id=f'{y}-{q+1}',start_ms=ms(f'{y}-{q*3+1:02}-01T00:00:00+00:00'),end_ms=ms(f'{y+1}-01-01T00:00:00+00:00') if q==3 else ms(f'{y}-{(q+1)*3+1:02}-01T00:00:00+00:00')) for y in (2021,2022) for q in range(4)]
    oldranges=[r for r in manifest['items'] if r['symbol'] in SYMBOLS and r['compatible_spot']]
    reuse_start=max(r['first_ms'] for r in oldranges)+25*H;reuse_end=min(r['last_ms'] for r in oldranges)-26*H+H
    blocks.append(dict(id='PR12_USED_ARCHIVE',start_ms=reuse_start,end_ms=reuse_end))
    now=utc();spec=dict(id=key,registered_at=now,deadline=(instant(now)+timedelta(days=1)).isoformat(),source_version=version(),manifest_hash=digest(manifest),classification='USED_HISTORICAL_EXPANSION_EXPLORATORY_USE_HISTORY_UNKNOWN_NOT_UNTOUCHED_OOS',
        universe=list(SYMBOLS),rules=RULES,costs=COSTS,entry_delay_hours=2,horizons=[4,24],lockout_hours=26,blocks=blocks,
        historical_range=[start,end],reused_range=[reuse_start,reuse_end],padding=dict(warmup_hours=25,label_hours=26),
        acquisition=dict(requests=requests,expected_requests=len(requests),max_http_attempts=90,max_retries_per_request=2,estimated_bytes=14*1024*1024,max_bytes=24*1024*1024,max_seconds=240,estimated_cpu_seconds=25,cached_funding=cached_funding,
            chronological_selection='First two full calendar years from 2021; adding 2023 needs 81 spot + funding + 9 retry allowance >90 attempts.',budget_blocked=['2023-01-01T00:00:00Z','2026-09-27T07:00:00Z']),
        statistics=dict(unit='TRANSITIVE_26H_CLUSTER_ACROSS_BOTH_MECHANISMS',cross_block='Assign complete cluster to earliest signal block; carry lockouts across blocks. Never reset at quarter boundaries.',within_cluster='Equal weight mechanisms present; equal accepted events within mechanism; equal weight clusters.',zero_trades='Zero cash PnL and zero exposure; absent clusters give null mean, not zero evidence.',missing='Exclude incomplete common clocks for every arm and disclose reason; no imputation.',ties='Lexicographic symbol after ascending prior24h return',periods='All fixed quarterly blocks including zero signal blocks',confidence_interval=None,reason_no_CI='Exploratory directional diagnostics, no calibrated independence/power claim',minimum_clusters=10,
            verdict='N<10 => INCONCLUSIVE; otherwise mean<=0 => REJECT for that axis; positive => CANDIDATE_FOR_FRESH_VALIDATION only. 4h additionally requires positive absolute and selection means. No timing verdict inferred.'),
        timing_contribution='TIMING_CONTRIBUTION_UNRESOLVED',new_alpha_allocations=0,model_calls=0)
    if len(requests)>81:raise ValueError('FROZEN_REQUESTS_EXCEED_CAP_WITH_RETRY_ALLOWANCE')
    s.append('expansion_manifest',key,dict(id=key,manifest=manifest))
    return s.append('expansion_spec',key,spec)

def decode(root,r,kind):
    raw=(root/'raw'/(r['sha256']+'.bin')).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('RAW_HASH')
    if raw[:2]==b'PK':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names=[n for n in z.namelist() if n.endswith('.csv')]
            if len(names)!=1:raise ValueError('CSV_MEMBER')
            rows=list(csv.DictReader(io.StringIO(z.read(names[0]).decode('utf8'))))
        return [dict(fundingTime=int(float(x['calc_time'])),fundingRate=x['last_funding_rate']) for x in rows]
    return json.loads(raw)

def load(s,spec,bindings,quarantine=None):
    data={symbol:dict(bars={},funding={}) for symbol in SYMBOLS}
    for x in bindings:
        r=x['receipt'];kind=x['kind'];rows=decode(s.root,r,kind);d=data[x['symbol']][kind]
        for row in rows:
            t=row[0] if kind=='bars' else row['fundingTime']
            if type(t)!=int:raise ValueError('TIMESTAMP_TYPE')
            if kind=='bars':
                if len(row)<11 or row[6]>=ms(r['received_at']):raise ValueError('BAR_INTERVAL_OR_FUTURE')
                if row[6]!=t+H-1:
                    if not t<=row[6]<t+H-1:raise ValueError('INVALID_BAR_INTERVAL')
                    if quarantine is None:raise ValueError('SHORT_BAR_REQUIRES_EXPLICIT_QUARANTINE')
                    quarantine.append(dict(symbol=x['symbol'],open_ms=t,close_ms=row[6],receipt_hash=r['sha256'],reason='SHORT_TERMINAL_BAR_EXCLUDED_NOT_EXTENDED'))
                    continue
                if any(isinstance(row[i],bool) or not math.isfinite(float(row[i])) or float(row[i])<0 for i in (1,2,3,4,5,7,9,10)):raise ValueError('BAR_VALUE')
                if min(float(row[i]) for i in (1,2,3,4))<=0 or float(row[10])>float(row[7])+1e-7:raise ValueError('PRICE_OR_TAKER')
            else:
                if not math.isfinite(float(row['fundingRate'])) or t>ms(r['received_at']):raise ValueError('FUNDING_VALUE_OR_FUTURE')
                row=dict(fundingTime=t,fundingRate=str(row['fundingRate']))
            if t in d and d[t]!=row:
                # Funding representations may differ only in decimal formatting.
                if kind!='funding' or float(d[t]['fundingRate'])!=float(row['fundingRate']):raise ValueError('CONFLICTING_DUPLICATE')
            d[t]=row
    return {symbol:{k:[v[t] for t in sorted(v)] for k,v in d.items()} for symbol,d in data.items()}

def trade(bars,symbol,t,h):
    en=float(bars[symbol][t+2*H][1]) if symbol else None;ex=float(bars[symbol][t+(2+h)*H][1]) if symbol else None
    qty=1250/(en*1.0016) if symbol else 0.;gross=qty*(ex-en) if symbol else 0.;turnover=qty*(en+ex) if symbol else 0.
    fee=turnover*.001;slip=turnover*.0005;spread=turnover*.0001
    return dict(symbol=symbol,action='LONG' if symbol else 'NO_TRADE',entry_ms=t+2*H,exit_ms=t+(2+h)*H,entry_price=en,exit_price=ex,quantity=qty,gross_usdt=gross,fee_usdt=fee,slippage_usdt=slip,spread_usdt=spread,cost_usdt=fee+slip+spread,net_usdt=gross-fee-slip-spread,turnover_usdt=turnover,exposure_hours=h if symbol else 0,capital_occupied_hours=26 if symbol else 0,trades=int(bool(symbol)),information_cost_usdt=0,funding_usdt=0,not_a_fill=True)

def calculate(s,spec,bindings):
    quarantine=[];data=load(s,spec,bindings,quarantine);cs,excluded=contexts(data);ranges=[spec['historical_range'],spec['reused_range']]
    allowed=lambda t:any(a<=t<b for a,b in ranges)
    cs=[c for c in cs if allowed(c['signal_ms'])];excluded=[r for r in excluded if allowed(r['signal_ms'])]
    bars={symbol:{r[0]:r for r in d['bars']} for symbol,d in data.items()};labels=clusters(cs,digest(bindings))
    block=lambda t:next(b['id'] for b in spec['blocks'] if b['start_ms']<=t<b['end_ms'])
    cluster_blocks={}
    for c in cs:
        if c['signal_ms'] in labels:cluster_blocks.setdefault(labels[c['signal_ms']],block(c['signal_ms']))
    events=[];full=[];signals=[];busy={m:-1 for m in (ABSORPTION,DELAY,'PRICE_ONLY')}
    for c in cs:
        t=c['signal_ms'];p=price(c)
        if p and t>=busy['PRICE_ONLY']:
            busy['PRICE_ONLY']=t+26*H;full.append(dict(signal_ms=t,block=block(t),paths={str(h):trade(bars,p,t,h) for h in (4,24)}))
        for mechanism in (ABSORPTION,DELAY):
            symbol=select(c,mechanism)
            if not symbol:continue
            accepted=t>=busy[mechanism]
            signals.append(dict(mechanism=mechanism,signal_ms=t,symbol=symbol,accepted=accepted,cluster_id=labels[t]))
            if not accepted:continue
            busy[mechanism]=t+26*H
            events.append(dict(mechanism=mechanism,signal_ms=t,block=block(t),cluster_block=cluster_blocks[labels[t]],cluster_id=labels[t],features=c['assets'],arms={a:{str(h):trade(bars,symbol if a=='ECONOMIC_SELECTION' else p if a=='PRICE_SELECTOR' else 'BTCUSDT' if a=='BTC' else None,t,h) for h in (4,24)} for a in ARMS}))
    metrics=('gross_usdt','fee_usdt','slippage_usdt','spread_usdt','cost_usdt','net_usdt','turnover_usdt','exposure_hours','capital_occupied_hours','trades','information_cost_usdt','funding_usdt')
    aggregate=lambda paths:{k:sum(p[k] for p in paths) for k in metrics}
    summaries={}
    for bid in ['ALL']+[b['id'] for b in spec['blocks']]:
        subset=[e for e in events if bid=='ALL' or e['block']==bid]
        summaries[bid]={m:{a:{str(h):aggregate([e['arms'][a][str(h)] for e in subset if e['mechanism']==m]) for h in (4,24)} for a in ARMS} for m in (ABSORPTION,DELAY)}
        summaries[bid]['PRICE_ONLY']={str(h):aggregate([e['paths'][str(h)] for e in full if bid=='ALL' or e['block']==bid]) for h in (4,24)}
    cluster_scores=[]
    for cid in dict.fromkeys(e['cluster_id'] for e in events):
        rows=[e for e in events if e['cluster_id']==cid];mechanisms=sorted({e['mechanism'] for e in rows});scores={}
        for h in (4,24):
            mean=lambda a:statistics.mean(statistics.mean(e['arms'][a][str(h)]['net_usdt'] for e in rows if e['mechanism']==m) for m in mechanisms)
            scores[str(h)]=dict(economic_net=mean('ECONOMIC_SELECTION'),selection_increment=mean('ECONOMIC_SELECTION')-mean('PRICE_SELECTOR'),btc_difference=mean('ECONOMIC_SELECTION')-mean('BTC'))
        cluster_scores.append(dict(id=cid,block=rows[0]['cluster_block'],mechanisms=mechanisms,events=len(rows),scores=scores))
    n=len(cluster_scores);means={str(h):{a:statistics.mean(r['scores'][str(h)][a] for r in cluster_scores) if n else None for a in ('economic_net','selection_increment','btc_difference')} for h in (4,24)}
    def verdict(values):return 'INCONCLUSIVE' if n<10 else 'REJECT' if any(v<=0 for v in values) else 'CANDIDATE_FOR_FRESH_VALIDATION'
    verdicts=dict(unchanged_24h=verdict([means['24']['economic_net']]),short_4h=verdict([means['4']['economic_net'],means['4']['selection_increment']]),selection_4h=verdict([means['4']['selection_increment']]),selection_24h=verdict([means['24']['selection_increment']]),timing='TIMING_CONTRIBUTION_UNRESOLVED')
    # Full policy differences remain separate per sleeve; never sum duplicated
    # price baselines or interpret timing/frequency/exposure mixtures as alpha.
    differences={m:{str(h):summaries['ALL'][m]['ECONOMIC_SELECTION'][str(h)]['net_usdt']-summaries['ALL']['PRICE_ONLY'][str(h)]['net_usdt'] for h in (4,24)} for m in (ABSORPTION,DELAY)}
    expected=sum((b-a)//H for a,b in ranges);missing_clocks=expected-len(cs)
    return dict(status='HISTORICAL_EXPANSION_COMPLETED',classification=spec['classification'],bindings=bindings,events=events,signals=signals,full_price_policy=full,summaries=summaries,cluster_scores=cluster_scores,cluster_means=means,verdicts=verdicts,whole_policy_net_gap=differences,
        counts=dict(expected_clocks=expected,complete_clocks=len(cs),missing_clocks=missing_clocks,accepted_economic_events=len(events),temporal_clusters=n,price_policy_trades=len(full),new_forward_events=0,paper_fills=0,model_calls=0),coverage_exclusions=excluded,
        period_coverage={b['id']:dict(expected=(b['end_ms']-b['start_ms'])//H,complete=sum(b['start_ms']<=c['signal_ms']<b['end_ms'] for c in cs)) for b in spec['blocks']},
        quarantined_bars=quarantine,missing_policy='Expected clocks absent from intersection also counted missing; no imputed labels',interpretation='Equal-cluster pooled directional diagnostics, not a portfolio or significance test. Whole-policy gap confounds timing/frequency/exposure/cost. Used archive remains exploratory.')

def repair(s):
    old=s.all('expansion_spec')[0];failure=s.get('extension_operation',old['id'])
    if failure['success'] or failure['result'].get('error')!='ValueError:BAR_INTERVAL_OR_FUTURE':raise ValueError('ONLY_RECORDED_SHORT_BAR_FAILURE_REPAIR')
    key=old['id']+'-quarantine-v2';prior=s.get('expansion_spec',key,True)
    if prior:return prior
    body={k:v for k,v in old.items() if k not in ('id','stored_at')}
    body.update(id=key,source_version=version(),registered_at=utc(),reuse_operation_id=old['id'],parent_spec_hash=digest(old),parent_failure_hash=digest(failure),
        repair_reason='Preserve failed strict reader. Quarantine raw short terminal bars; do not extend, impute or relax any economic gate. Use exactly the original cached requests, no new collection.',
        input_binding=[r['receipt'] for r in s.all('extension_input') if r['id'].startswith(old['id']+':')])
    s.append('expansion_manifest',key,dict(id=key,manifest=s.get('expansion_manifest',old['id'])['manifest']))
    return s.append('expansion_spec',key,body)

def run(root):
    with process_lock(root):
        s=Store(root)
        try:
            spec=s.all('expansion_spec')[-1]
            if spec['source_version']!=version():raise ValueError('EXPANSION_SOURCE_CHANGED')
            def execute(started):
                inp=Inputs(s,spec['id'],started,240);bindings=[];errors=[]
                manifest=s.get('expansion_manifest',spec['id'])['manifest']
                for r in manifest['items']:
                    if r['symbol'] in SYMBOLS and (r['compatible_spot'] or r.get('fields')==['fundingRate','fundingTime'] or r in spec['acquisition']['cached_funding'][r['symbol']]):
                        if (s.root/'raw'/(r['sha256']+'.bin')).exists():bindings.append(dict(symbol=r['symbol'],kind='bars' if r['compatible_spot'] else 'funding',receipt=r))
                for request in spec['acquisition']['requests']:
                    try:
                        kind=request['kind'];params=dict(symbol=request['symbol'],startTime=request['start'],endTime=request['end'],limit=1000)
                        if kind=='bars':params['interval']='1h'
                        host=SPOT if kind=='bars' else FUTURES;path='/api/v3/klines' if kind=='bars' else '/fapi/v1/fundingRate'
                        if spec.get('reuse_operation_id'):
                            from urllib.parse import urlencode
                            cachekey=spec['reuse_operation_id']+':'+digest(host+path+'?'+urlencode(params))
                            r=s.get('extension_input',cachekey)['receipt']
                        else:r=inp.get(host,path,params)
                        bindings.append(dict(symbol=request['symbol'],kind=kind,receipt=r))
                    except Exception as exc:
                        errors.append(dict(request=request,error=str(exc)))
                        if any(k in str(exc) for k in ('CAP','429','418')):break
                result=calculate(s,spec,bindings);result['acquisition_errors']=errors
                return s.append('expansion_result',spec['id'],dict(id=spec['id'],spec_hash=digest(spec),completed_at=utc(),**result))
            return owned(s,spec['id'],spec,240,execute)
        finally:s.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--runtime',type=Path,required=True);ap.add_argument('action',choices=['register','repair','run']);ap.add_argument('--manifest',type=Path);a=ap.parse_args()
    if a.action in ('register','repair'):
        with process_lock(a.runtime):
            s=Store(a.runtime)
            try:r=repair(s) if a.action=='repair' else register(s,json.loads(a.manifest.read_bytes()))
            finally:s.close()
    else:r=run(a.runtime)
    print(json.dumps(r,ensure_ascii=False,indent=2))
