"""Network-disabled synthetic follow-up evidence; fresh runtimes only."""
import argparse,json,shutil,socket,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from one_sentinel.engine import Observer
from one_sentinel.demo import T,SYMBOL,metadata,mini,bar,book,sequence

def stamp(at,session='A'):
    return dict(wall=at,monotonic=10+(at-T) if session=='A' else 1+(at-T-7),session_id=session)
def put(o,s,t,v,**kw):return o.ingest(s,json.dumps(v).encode(),t,t,**kw)
def calibration(o):put(o,'clock',T+.6,dict(serverTime=int((T+.3)*1000)),timing=stamp(T+.6),context=dict(request_timing=stamp(T)))
def uncertain(o):
    put(o,'metadata',T,metadata('BREAK'));calibration(o);put(o,'metadata',T+.7,metadata('TRADING'),timing=stamp(T+.7))
    for t in (T+1,T+1.1,T+1.2):put(o,'mini',t,mini(t-.01),timing=stamp(t))
    put(o,'bar',T+1.3,bar(T+1.29),timing=stamp(T+1.3))

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);a=p.parse_args();a.out.mkdir(exist_ok=False)
    def blocked(*args,**kwargs):raise AssertionError('OFFLINE_SYNTHETIC_ONLY')
    socket.socket.connect=blocked;socket.create_connection=blocked
    results={}
    for name in ('release_tick','release_calibration','exit_structure','exit_horizon','partial_frame','ttl','expired','stale_bar','new_epoch','wall_jump','future_invalid'):
        o=Observer(a.out/name/'runtime')
        if name.startswith('release_'):
            uncertain(o)
            assert not any(d['action']=='ENTER' for d in o.l.all('decision')) and not o.l.all('fill')
            if name=='release_tick':o.tick(T+2,stamp(T+2));ready=T+4
            else:
                put(o,'clock',T+1.6,dict(serverTime=int((T+1.595)*1000)),timing=stamp(T+1.6),context=dict(request_timing=stamp(T+1.59)));ready=T+3.6
            put(o,'book',ready-.01,book(),timing=stamp(ready-.01));assert not o.l.all('fill')
            put(o,'book',ready,book(),timing=stamp(ready));assert len(o.l.all('fill'))==1
        elif name.startswith('exit_'):
            for s,t,d in sequence()[:5]:put(o,s,t,d)
            if name=='exit_structure':at=T+6;put(o,'metadata',at,metadata('BREAK'),timing=stamp(at))
            else:at=T+5+o.policy['horizon_seconds'];o.tick(at,stamp(at))
            o.close();o=Observer(a.out/name/'runtime')
            for delta in (2,3,4,4,14):put(o,'book',at+delta,book('94'),timing=stamp(at+delta,'B'))
            assert [f['action'] for f in o.l.all('fill')]==['ENTER','EXIT']
            assert o.status()['positions'][SYMBOL]['qty']=='0.000'
        elif name=='partial_frame':
            calibration(o);put(o,'mini',T+1.3,[dict(mini(T+.9)[0],s='VALIDUSDT'),mini(T+1.29)[0]],timing=stamp(T+1.3));o.tick(T+2,stamp(T+2));o.tick(T+2,stamp(T+2))
        else:
            if name=='stale_bar':put(o,'metadata',T,metadata('BREAK'))
            calibration(o)
            if name=='future_invalid':put(o,'mini',T+1.3,mini(T+10)+[{}],timing=stamp(T+1.3));o.tick(T+20,stamp(T+20))
            elif name=='expired':put(o,'mini',T+120,mini(T+119.99),timing=stamp(T+120));o.tick(T+121,stamp(T+121))
            else:
                if name=='stale_bar':
                    put(o,'metadata',T+.7,metadata('TRADING'));v=bar(T+1.29);v['k'].update(t=int((T-240)*1000),T=int((T-180)*1000)-1);put(o,'bar',T+1.3,v,timing=stamp(T+1.3))
                else:put(o,'mini',T+1.3,mini(T+1.29),timing=stamp(T+1.3))
                if name=='ttl':o.tick(T+6.3,stamp(T+6.3))
                elif name=='new_epoch':o.close();o=Observer(a.out/name/'runtime');o.tick(T+2,stamp(T+2,'B'))
                elif name=='wall_jump':o.tick(T+2,dict(wall=T+2,monotonic=11.5,session_id='A'))
                else:o.tick(T+2,stamp(T+2))
            assert not o.l.all('interpretation_release')
        dest=a.out/name/'export';o.export(dest);shutil.copytree(o.l.root/'raw',dest/'raw');o.close()
        shutil.copyfile(a.out/name/'runtime/observer.sqlite3',dest/'observer.sqlite3')
        run=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).with_name('verify_sentinel_corrections.py')),str(dest)],capture_output=True,text=True,encoding='utf-8')
        (a.out/name/'verify_stdout.txt').write_text(run.stdout,encoding='utf-8');(a.out/name/'verify_stderr.txt').write_text(run.stderr,encoding='utf-8')
        assert run.returncode==0,(name,run.stderr)
        rows=[json.loads(x) for x in (dest/'ledger.jsonl').read_text().splitlines()]
        timeline=[dict(sequence=r['seq'],kind=r['kind'],**json.loads(r['body'])) for r in rows if r['kind'] in ('receipt','interpretation','clock_pending','clock_pending_result','interpretation_release','decision','decision_retry','execution','fill')]
        (a.out/name/'timeline.json').write_text(json.dumps(timeline,indent=2),encoding='utf-8')
        results[name]=dict(classification='SYNTHETIC_OFFLINE_NOT_MARKET',verification=json.loads(run.stdout))
    (a.out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8');print(json.dumps({k:v['verification']['record_counts'] for k,v in results.items()},indent=2))
if __name__=='__main__':main()
