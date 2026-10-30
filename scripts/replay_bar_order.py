"""Fixed synthetic counterexample. No policy-derived fixture values or network.

Same original kline bytes before/after. Assertions live in the regression tests;
this exporter also records the failing baseline without silently rewriting it.
"""
import argparse,hashlib,json,shutil,socket,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from one_sentinel.engine import Observer
from one_sentinel.demo import metadata

T=1800000000
SYMBOL='NEWUSDT'
def stamp(at):return dict(wall=at,monotonic=10+at-T,session_id='BAR_ORDER_SYNTHETIC_EPOCH')
def bar(event,opened,volume,symbol=SYMBOL):
    return dict(s=symbol,E=round(event*1000),k=dict(i='1m',x=True,t=round(opened*1000),T=round(opened*1000)+59999,q=volume))
def fixture(case):
    rows=[]
    def put(source,at,value,context=None):rows.append(dict(method='ingest',source=source,at=at,value=value,context=context,timing=stamp(at)))
    put('metadata',T,metadata('BREAK'))
    put('clock',T+.6,dict(serverTime=round((T+.3)*1000)),dict(request_timing=stamp(T)))
    put('metadata',T+.7,metadata('TRADING'))
    if case in ('delayed','other_symbol'):
        put('bar',T+59.98,bar(T+59.97,T-60,'1000','BTCUSDT' if case=='other_symbol' else SYMBOL))
    put('bar',T+60.8,bar(T+60.4,T,'99000'))
    if case=='ordinary':put('bar',T+60.9,bar(T+60.5,T-60,'1000'))
    rows.append(dict(method='tick',at=T+61,timing=stamp(T+61)))
    put('mini',T+61.1,[dict(e='24hrMiniTicker',s=SYMBOL,E=round((T+60.7)*1000),c='100',v='1000',q='100000')])
    return rows
def run(root,case):
    o=Observer(root);rows=fixture(case)
    for r in rows:
        if r['method']=='tick':o.tick(r['at'],r['timing'])
        else:o.ingest(r['source'],json.dumps(r['value'],sort_keys=True).encode(),r['at'],r['at'],timing=r['timing'],context=r['context'])
    decision=o.l.last('decision');latest=o.l.latest_input('bar',SYMBOL)
    result=dict(case=case,classification='SYNTHETIC_OFFLINE_NOT_MARKET',fixture_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
        action=decision['action'],reason=decision['reason'],latest_bar=latest['parsed'],latest_bar_input_id=latest['id'],
        pending_outcomes=o.l.all('clock_pending_result'),release_count=len(o.l.all('interpretation_release')),
        quarantines=o.l.all('quarantine'),source_hash=o.l.get('contract','v1')['source_hash'])
    return o,result,rows
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(exist_ok=False)
    def blocked(*args,**kw):raise AssertionError('OFFLINE_ONLY')
    socket.socket.connect=blocked;socket.create_connection=blocked
    results={}
    for name in ('control','delayed','ordinary','other_symbol'):
        o,result,rows=run(a.out/name/'runtime',name)
        export=a.out/name/'export';o.export(export);shutil.copytree(o.l.root/'raw',export/'raw');o.close()
        shutil.copyfile(a.out/name/'runtime/observer.sqlite3',export/'observer.sqlite3')
        (a.out/name/'fixture.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
        (a.out/name/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');results[name]=result
    (a.out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps(results,indent=2))
if __name__=='__main__':main()
