"""Separate process: independent formulas, source marks, full replay and prefix QA."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.historical_data import load_histories, ms, write_json
from one_trading.observer import DAY
from one_trading.walkforward import EXCLUDED, IDENTITY_BOUNDARIES
from scripts.validate_cashflow_alpha import close, recompute, check_reported_summary, audit_market_fills
from scripts.validate_cashflow_v2 import daily_ledger
from scripts.run_repricing_v3 import file_sha, validate_freeze


def eq_tree(a,b):
    if isinstance(a,dict):
        if a.keys()!=b.keys():
            raise AssertionError('keys_differ')
        for k in a:
            eq_tree(a[k],b[k])
    elif isinstance(a,float):
        close(a,b,'independent_label')
    elif a!=b:
        raise AssertionError(f'independent_label:{a}!={b}')


class IndependentSource:
    def __init__(self,h):
        self.h=h
        self.idx={s:{b.open_ms:i for i,b in enumerate(bs)} for s,bs in h.items()}
        self.by_day=defaultdict(list)
        self.age={}
        self.features_cache={}
        for s,bs in h.items():
            if s=='BTCUSDT' or s.removesuffix('USDT') in EXCLUDED:
                continue
            count=0
            for i,b in enumerate(bs):
                count=1 if i==0 or b.open_ms!=bs[i-1].open_ms+DAY or any(bs[i-1].open_ms<t<=b.open_ms for t in IDENTITY_BOUNDARIES.get(s,[])) else count+1
                if count>=240 and b.volume>0:
                    liquidity=sum(x.quote_volume for x in bs[i-29:i+1])/30
                    if liquidity>=1e6:
                        self.by_day[b.open_ms].append((s,liquidity))
                        self.age[(s,b.open_ms)]=count
        self.universe={d:[s for s,_ in sorted(v,key=lambda x:(-x[1],x[0]))] for d,v in self.by_day.items()}

    def features(self,s,day):
        key=(s,day)
        if key in self.features_cache:
            return self.features_cache[key]
        i=self.idx[s][day];bi=self.idx['BTCUSDT'][day]
        bs=self.h[s];btc=self.h['BTCUSDT'];p=bs[i].close
        avg=lambda xs:sum(xs)/len(xs)
        recent=sum(b.quote_volume for b in bs[i-6:i+1])/7
        prior=sum(b.quote_volume for b in bs[i-34:i-6])/28
        ranges=[max(bs[j].high-bs[j].low,abs(bs[j].high-bs[j-1].close),abs(bs[j].low-bs[j-1].close)) for j in range(i-69,i+1)]
        v=recent/prior if prior else 0.
        tr=avg(ranges[-14:])/avg(ranges[:-14]) if avg(ranges[:-14]) else 1.
        r28=p/bs[i-28].close-1
        r7=p/bs[i-7].close-1
        rel28=r28-(btc[bi].close/btc[bi-28].close-1)
        rel90=p/bs[i-90].close-btc[bi].close/btc[bi-90].close
        above=btc[bi].close>sum(b.close for b in btc[bi-199:bi+1])/200
        control=above and p>sum(b.close for b in bs[i-49:i+1])/50 and 0<=r28<=.30 and r7<=.15
        result=dict(demand=control and v>=1.5 and rel28>0,
            compression=control and tr<=.7 and p>max(b.high for b in bs[i-20:i]) and rel90>0,
            price_control=control, volume_ratio=v, tr_ratio=tr, relative28=rel28, relative90=rel90)
        self.features_cache[key]=result
        return result

    def path(self,s,first,ref,stop):
        # Independently collect the contiguous future path, then reduce slices.
        path=[];status=None
        for j in range(180):
            d=first+j*DAY
            i=self.idx[s].get(d)
            if i is None or d in IDENTITY_BOUNDARIES.get(s,[]):
                status='IDENTITY_BOUNDARY' if d in IDENTITY_BOUNDARIES.get(s,[]) else 'MISSING_OR_ENDED'
                break
            path.append(self.h[s][i])
        result={}
        for horizon in (90,180):
            bs=path[:horizon]
            ended=len(bs)<horizon
            result[f'{horizon}d_end_return']=-1. if ended else bs[-1].close/ref-1
            result[f'{horizon}d_max_return']=max([ref]+[b.high for b in bs])/ref-1
            result[f'{horizon}d_status']=status if ended else 'OBSERVED_THROUGH_HORIZON'
            for mult in (2,3):
                hit=next((i for i,b in enumerate(bs) if b.high>=mult*ref),None)
                target_path=bs[:hit+1] if hit is not None else bs
                low=min([ref]+[b.low for b in target_path])
                stopped=any(b.low<=stop for b in target_path)
                if hit is None and ended:
                    low=0.;stopped=True
                result[f'{horizon}d_{mult}x']=dict(hit=hit is not None,days=hit+1 if hit is not None else None,
                    mae=low/ref-1,stop_before_or_same_bar=stopped)
        return result


def verify_atlas(directory, source):
    rows=0;counts=Counter();previous=None
    expected=0
    end=ms('2026-10-01')
    for day,syms in source.universe.items():
        if ms('2019-01-01')<=day and day+181*DAY<end:
            expected+=len(syms)
    with gzip.open(directory/'OPPORTUNITY_ATLAS.jsonl.gz','rt',encoding='utf-8') as f:
        for line in f:
            r=json.loads(line);s=r['symbol'];day=r['day'];rows+=1
            if s not in source.universe[day] or day+181*DAY>=end:
                raise AssertionError('atlas_membership_or_maturity')
            key=(day,source.universe[day].index(s))
            if previous is not None and key<=previous:
                raise AssertionError('atlas_duplicate_or_order')
            previous=key
            b=source.h[s][source.idx[s][day]]
            eq_tree(source.path(s,day+DAY,b.close,.85*b.close),r['research'])
            entry=source.idx[s].get(day+2*DAY)
            if entry is not None and day+2*DAY not in IDENTITY_BOUNDARIES.get(s,[]):
                eb=source.h[s][entry]
                eq_tree(source.path(s,day+2*DAY,eb.open,.85*b.close),r['delayed'])
                if r['broker_gap_acceptable'] != (.90*b.close<=eb.open<=1.03*b.close):
                    raise AssertionError('entry_gap_classification')
            elif r['entry_available'] or r['delayed']['180d_2x']['hit'] or r['delayed']['180d_end_return']!=-1:
                raise AssertionError('missing_entry_label')
            counts[r['era']]+=1
            # Full feature calculation on every Sunday, all labels on every day.
            if (day//DAY+3)%7==6:
                actual=source.features(s,day)
                for name,value in actual.items():
                    eq_tree(value,r[name])
                if source.age[(s,day)]!=r['history_days']:
                    raise AssertionError('age_mismatch')
            if rows%100000==0:
                print(json.dumps(dict(phase='INDEPENDENT_LABELS', rows=rows)),flush=True)
    if rows!=expected:
        raise AssertionError('atlas_rows_missing')
    return dict(all_daily_labels_independently_recomputed=True, rows=rows,
                full_eligible_membership=True, era_rows=counts,
                all_sunday_signal_features_recomputed=True)


def verify_controls(directory):
    with gzip.open(directory/'inference_anchors.json.gz','rt',encoding='utf-8') as f:
        anchors=json.load(f)
    last={}
    for r in anchors:
        key=(r['symbol'],r['era'])
        if r['day']-last.get(key,-10**18)<182*DAY or r['boundary_purged']:
            raise AssertionError('overlapping_inference_anchor')
        last[key]=r['day']
    with gzip.open(directory/'MATCHED_CONTROLS.json.gz','rt',encoding='utf-8') as f:
        pairs=json.load(f)
    used=set();n=0
    for p in pairs:
        case=p['case']
        if not case['research']['180d_2x']['hit']:
            raise AssertionError('case_not_success')
        for control in p['controls']:
            key=(control['day'],control['symbol'])
            if key in used or control['research']['180d_2x']['hit']:
                raise AssertionError('reused_or_successful_failure_control')
            used.add(key);n+=1
            if any(case[k]!=control[k] for k in ('day','liquidity_bin','age_bin')):
                raise AssertionError('unmatched_covariate_bin')
    statistics=json.loads((directory/'atlas_statistics.json').read_text(encoding='utf-8'))
    for r in statistics['rates']:
        rows=[x for x in anchors if x['era']==r['era']]
        def signal(x):
            k=r['feature']
            return (True if k=='all' else x['volume_ratio']>=1.5 if k=='volume_ge_1_5' else
                    x['relative28']>0 if k=='relative28_positive' else x['tr_ratio']<=.7 if k=='tr_le_0_7' else
                    x['btc_above'] if k=='btc_above_sma200' else x[k])
        signals=[x for x in rows if signal(x)]
        successes=sum(x['research'][r['label']]['hit'] for x in rows)
        sy=sum(x['research'][r['label']]['hit'] for x in signals)
        if (len(rows),len(signals),successes,sy)!=(r['n'],r['signals'],r['successes'],r['signal_successes']):
            raise AssertionError('probability_denominators')
    return dict(nonoverlapping_anchors=len(anchors), matched_cases=len(pairs), matched_failures=n,
                probability_denominators_checked=len(statistics['rates']))


def chronology_and_signals(result, source):
    from one_trading.repricing_v3 import ERAS
    strategy=result['config']['strategy'];expected=defaultdict(set);raw=defaultdict(set);last={}
    # Independent sequential episode selection over the full causal history.
    for day in sorted(source.universe):
        era=next((k for k,(a,z) in ERAS.items() if a<=day<z and day+181*DAY<z),None)
        if era is None or (day//DAY+3)%7!=6:
            continue
        for s in source.universe[day]:
            if source.features(s,day)[strategy]:
                raw[day].add(s)
                key=(s,era)
                if day-last.get(key,-10**18)>=182*DAY:
                    expected[day].add(s);last[key]=day
    due={}
    for p in result['plans']:
        d=p['signal_day_ms'];delay=result['config']['execution_delay']
        if p['execute_after_ms']!=d+delay*DAY or delay<2 or p['observed_ms']!=d+DAY:
            raise AssertionError('early_execution')
        if p['universe']!=source.universe.get(d,[]):
            raise AssertionError('plan_universe')
        if p['opportunity_count']!=len(expected[d]) or p['raw_qualifying_count']!=len(raw[d]):
            raise AssertionError('episode_count')
        reported={r['symbol'] for r in p['decisions'] if r['state'] in ('ENTER','WAIT_CAPACITY')}
        if reported!=expected[d]-set(p['position_context']):
            raise AssertionError('episode_candidates')
        if any(r['max_input_close_ms']>=p['observed_ms'] for r in p['decisions']):
            raise AssertionError('future_feature')
        for t in p['targets']:
            if t['symbol'] in p['position_context'] and t['allow_add']:
                raise AssertionError('adds_forbidden')
        due[p['execute_after_ms']]=p
    for f in result['fills']:
        if f['side']=='BUY':
            p=due[f['ts_ms']]
            if f['reason']!='OPEN_PROBE' or f['symbol'] not in {t['symbol'] for t in p['targets'] if t['allow_add']}:
                raise AssertionError('unplanned_buy')
            i=source.idx[f['symbol']][f['ts_ms']]
            close(source.h[f['symbol']][i].open,f['reference_price'],'raw_open_fill')
    return dict(episode_rules_and_delay=True,plans=len(result['plans']))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--freeze',type=Path,required=True)
    a=p.parse_args()
    validate_freeze(a.freeze)
    manifest=json.loads((a.run/'artifact_hashes.json').read_text(encoding='utf-8'))
    for path,digest in manifest.items():
        if file_sha(a.run/path)!=digest:
            raise AssertionError('artifact_hash_mismatch:'+path)
    h,_=load_histories(a.archive);source=IndependentSource(h)
    report=dict(passed=False,artifact_hashes=True,atlas=verify_atlas(a.run,source),
                controls=verify_controls(a.run),accounts={})
    from one_trading.repricing_v3 import AtlasPanel,config_for,run
    panel=AtlasPanel(h,config_for('price_control')).prepare()
    cut=ms('2023-01-01')
    truncated={s:bs2 for s,bs in h.items() if (bs2:=[b for b in bs if b.open_ms<cut])}
    prefix_panel=AtlasPanel(truncated,config_for('price_control')).prepare()
    for path in sorted(a.run.glob('*__*__*.json')):
        result=json.loads(path.read_text(encoding='utf-8'))
        metrics=recompute(result)
        checks=check_reported_summary(result,metrics)
        checks.update(audit_market_fills(result,h))
        risk,holdings=daily_ledger(result,h)
        checks.update(risk)
        checks.update(chronology_and_signals(result,source))
        for plan in result['plans']:
            day=plan['signal_day_ms']
            if day>=ms(result['summary']['start']) and plan['position_context']!=holdings[day]:
                raise AssertionError('signal_position_context')
        if path.stem.endswith('__continuous'):
            c=result['config'];start=ms(result['summary']['start']);end=ms(result['summary']['end_exclusive'])
            repeated=run(panel,c,start,end)
            if repeated!=result:
                raise AssertionError('full_account_reproduction')
            short=run(prefix_panel,c,start,cut,liquidate_end=False)
            for key,time in {'equity':'ts_ms','fills':'ts_ms','writeoffs':'ts_ms','plans':'signal_day_ms'}.items():
                if short[key]!=[r for r in result[key] if r[time]<cut]:
                    raise AssertionError('future_prefix:'+key)
            checks.update(full_replay_identical=True,physical_future_deletion_prefix_identical=True,
                          prefix_days=len(short['equity']),prefix_fills=len(short['fills']))
        report['accounts'][path.stem]=checks
        print(json.dumps(dict(phase='INDEPENDENT_ACCOUNT',account=path.stem)),flush=True)
        write_json(a.run/'independent_review.partial.json',report)
    report['passed']=True
    write_json(a.run/'independent_review.json',report)
    print(json.dumps(dict(phase='INDEPENDENT_REVIEW_COMPLETE',accounts=len(report['accounts']))),flush=True)


if __name__=='__main__':
    main()
