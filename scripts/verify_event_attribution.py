"""Independent raw-byte matched-clock verifier. No production imports or network."""
import argparse,hashlib,json,math
from pathlib import Path
from datetime import datetime
from verify_event_scan import verify_event

H=3600000
def sha(x):return hashlib.sha256(x).hexdigest()
def digest(x):return sha(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode())
def near(x,y):
    assert type(x) in (int,float) and math.isfinite(x) and math.isclose(x,y,rel_tol=1e-10,abs_tol=1e-8),(x,y)

def verify(root):
    root=Path(root);rows=[json.loads(x) for x in (root/'ledgers/batch.jsonl').read_text('utf-8').splitlines()]
    kinds=lambda k:[r['body'] for r in rows if r['kind']==k]
    parents={r['id']:r for r in kinds('hypothesis_trial') if r.get('action')=='EVALUATE_EVENTS'}
    specs={r['id']:r for r in kinds('hypothesis_spec')}
    artifacts=kinds('event_attribution_result');assert len(artifacts)==1
    r=artifacts[0];reg=next(x for x in kinds('event_attribution_spec') if x['id']==r['id'])
    assert r['spec_hash']==digest(reg)
    assert datetime.fromisoformat(reg['registered_at'])<=datetime.fromisoformat(r['completed_at'])
    assert reg['parents']==[dict(id=p['id'],hash=digest(p)) for p in parents.values()]
    accepted={};raw={};source_hashes=set()
    for p in parents.values():
        verify_event(root,specs[p['id']],p)
        for symbol,pair in p['source_receipts'].items():
            for receipt in pair:
                payload=(root/'raw/batch'/(receipt['sha256']+'.bin')).read_bytes()
                assert sha(payload)==receipt['sha256'] and len(payload)==receipt['bytes']
                source_hashes.add(receipt['sha256'])
            raw[symbol]={v[0]:v for v in json.loads((root/'raw/batch'/(pair[0]['sha256']+'.bin')).read_bytes())}
        for x in p['rows']:
            if x['arm']=='ECONOMIC_RULE' and x.get('symbol'):accepted[p['id'],x['signal_ms']]=x
    assert len(accepted)==len(r['events'])==5
    seen=set();groups={};positive4=net_positive4=positive24=0
    for e in r['events']:
        key=(e['parent_trial_id'],e['signal_ms']);assert key not in seen;seen.add(key);old=accepted[key];parent=parents[key[0]]
        assert e['parent_result_hash']==digest(parent) and e['source_receipts']==parent['source_receipts']
        assert e['entry_ms']==old['entry_ms'] and e['cluster_id']==old['cluster_id']
        assert e['historical_feature_time_ms']==e['signal_ms']-1
        assert e['actually_received_at']==specs[key[0]]['input_binding']['available_at']
        # Independently rebuild the price-only rank from completed raw bars.
        t=e['signal_ms'];past={s:float(b[t-H][4])/float(b[t-24*H][1])-1 for s,b in raw.items()}
        rank=min(past,key=lambda s:(past[s],s));price=rank if past[rank]<0 else None
        chosen={'ECONOMIC_SELECTION':old['symbol'],'BTC':'BTCUSDT','PRICE_SELECTOR':price,'CASH':None}
        assert set(e['arms'])==set(chosen)
        for arm,symbol in chosen.items():
            a=e['arms'][arm];assert a['symbol']==symbol and a['action']==('LONG' if symbol else 'NO_TRADE')
            en=e['entry_ms'];bars=raw[symbol] if symbol else None;entry=float(bars[en][1]) if bars else None
            qty=1250/(entry*1.0016) if bars else 0.
            near(a['quantity'],qty);near(a['nominal_opportunity_usdt'],1250);near(a['cash_deployed_usdt'],1250 if bars else 0)
            if bars:near(a['entry_price'],entry);near(a['preentry_2h_return'],entry/float(bars[en-2*H][1])-1)
            else:assert a['entry_price'] is None and a['preentry_2h_return'] is None
            assert [m['hours'] for m in a['markouts']]==[1,2,4,8,12,24]
            for m in a['markouts']:
                h=m['hours'];assert m['exit_ms']==en+h*H
                ex=float(bars[en+h*H][1]) if bars else None;turn=qty*(entry+ex) if bars else 0
                gross=qty*(ex-entry) if bars else 0.;fee=turn*.001;slip=turn*.0005;spread=turn*.0001
                # Second accounting form: sale proceeds minus initial cash.
                net=qty*ex*.9984-1250 if bars else 0.
                near(m['net_usdt'],net);near(m['gross_usdt'],gross)
                near(m['fee_usdt'],fee);near(m['slippage_usdt'],slip);near(m['spread_usdt'],spread);near(m['trading_cost_usdt'],fee+slip+spread)
                near(m['price_return'],ex/entry-1 if bars else 0.)
                if bars:near(m['exit_price'],ex)
                else:assert m['exit_price'] is None
                near(m['mae'],min([0.]+[float(bars[z][3])/entry-1 for z in range(en,en+h*H,H)]) if bars else 0.)
                near(m['mfe'],max([0.]+[float(bars[z][2])/entry-1 for z in range(en,en+h*H,H)]) if bars else 0.)
                assert m['information_cost_usdt']==0 and m['funding_usdt']==0
                for gkey in (e['mechanism_id'],e['cluster_id']):
                    g=groups.setdefault(gkey,dict(events=set(),arms={a:{str(n):dict(gross_usdt=0.,cost_usdt=0.,net_usdt=0.) for n in [1,2,4,8,12,24]} for a in chosen}))
                    g['events'].add(e['id']);z=g['arms'][arm][str(h)];z['gross_usdt']+=gross;z['cost_usdt']+=fee+slip+spread;z['net_usdt']+=net
        selected=e['arms']['ECONOMIC_SELECTION']['markouts'];btc=e['arms']['BTC']['markouts']
        for value,x,y in zip(e['selected_minus_BTC_net_usdt'],selected,btc):near(value,x['net_usdt']-y['net_usdt'])
        positive4+=selected[2]['price_return']>0;net_positive4+=selected[2]['net_usdt']>0;positive24+=selected[-1]['price_return']>0
        near(selected[-1]['net_usdt'],old['net_usdt'])
    assert seen==set(accepted) and set(groups)==set(r['groups'])
    for k,g in groups.items():
        assert r['groups'][k]['events']==len(g['events'])
        for a,hs in g['arms'].items():
            for h,values in hs.items():
                for field,value in values.items():near(r['groups'][k]['arms'][a][h][field],value)
    assert r['original_full_policy_baselines']=={p['id']:p['aggregates']['PRICE_ONLY'] for p in parents.values()}
    assert r['counts']==dict(accepted_economic_events=5,temporal_clusters=5,reused_raw_files=6,new_data_collections=0,model_attempts=0,synthetic_market_results=0,paper_fills=0,new_forward_events=0,new_mature_forward_results=0)
    designs=kinds('event_followup_design');assert len(designs)==2
    for d in designs:
        assert d['parent_artifact_hash']==digest(r) and d['origin']=='POST_HOC_GENERATED' and d['auto_execution'] is False
        assert datetime.fromisoformat(d['start_signal_at'])>datetime.fromisoformat(d['registered_at'])>=datetime.fromisoformat(r['completed_at'])
        assert d['model_calls']==0 and d['horizons_seconds']==[14400,86400]
    authority=json.loads((root/'authority.json').read_bytes())
    task=next(t for t in authority['tasks'] if t['task_key']==r['id'])
    assert task['state']=='DONE' and task['actual_time']==r['elapsed_seconds'] and task['actual_cash']==0
    assert json.loads(task['result_json'])==r
    assert json.loads(task['task_json'])['input_hash']==digest(reg)
    assert not any(x['experiment_id']==r['id'] for x in authority['alpha_registry'])
    return dict(status='PASS_RAW_MATCHED_EVENT_ATTRIBUTION',raw_files=len(source_hashes),accepted_events=5,arms_per_event=4,horizons_per_arm=6,
        price_positive_4h=positive4,net_positive_4h=net_positive4,price_positive_24h=positive24,new_independent_evidence=0,followup_designs=2)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args();print(json.dumps(verify(a.bundle),indent=2))
