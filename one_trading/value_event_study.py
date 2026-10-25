"""Outcome-blind event chronology and matching; research-only atlas linkage."""
from __future__ import annotations
from collections import Counter
import math
import random
import statistics
from .observer import DAY
from .repricing_v3 import END, ERAS, era_of, mature_in_era
from .value_evidence import timestamp, iso

LABELS=('90d_2x','90d_3x','180d_2x','180d_3x')


def clocks(event):
    published=event.get('published_at')
    if not published or not event.get('source_time_proven') or not event.get('direct_value_accrual'):
        return dict(reconstructed_day=None, collector_day=None)
    available=timestamp(published)+3600000
    seen=timestamp(event['first_seen_at'])
    complete=timestamp(event.get('evidence_complete_at',event['first_seen_at']))
    return dict(available_after_reconstructed=iso(available),
                available_after_collector=iso(max(available,seen,complete)),
                reconstructed_day=available//DAY*DAY,
                collector_day=max(available,seen,complete)//DAY*DAY)


def schedule(events, mode='reconstructed'):
    """Never consult executed_at or any outcome to include an announcement.

    Establish episode suppression before maturity/history filtering: a missing
    label or excluded trade must not shift the next event forward opportunistically.
    """
    if mode not in ('reconstructed','collector'):
        raise ValueError('unknown_clock')
    result=[];last={}
    for e in sorted(events,key=lambda e:(e.get('published_at') or '9999',e['event_id'])):
        c=clocks(e);d=c[mode+'_day']; row=dict(event_id=e['event_id'],symbol=e['symbol'],mode=mode,**c)
        row['day']=d
        if d is None:
            row['exclusion']='QUALITATIVE_OR_INCIDENTAL'
        elif era_of(d) is None:
            row['exclusion']='OUTSIDE_STUDY_PERIOD'
        else:
            row['era']=era_of(d);key=(e['symbol'],row['era'])
            if d-last.get(key,-10**18)<182*DAY:
                row['exclusion']='DUPLICATE_EPISODE'
            else:
                last[key]=d
                if d+181*DAY>=END:
                    row['exclusion']='IMMATURE_180D'
                elif not mature_in_era(d):
                    row['exclusion']='ERA_BOUNDARY_PURGE'
                else:
                    row['exclusion']=None
            row['earliest_hypothetical_entry']=iso(d+2*DAY)
        result.append(row)
    return result


def exposed_symbols(events,day):
    exposed=set()
    for e in events:
        d=clocks(e)['reconstructed_day']
        if d is not None and 0<=day-d<=181*DAY:
            exposed.add(e['symbol'])
    return exposed


def select_controls(case,cohort,excluded=(),failures_only=False):
    """No outcome access in ordinary matching. Failure-only mode is descriptive."""
    eligible=[r for r in cohort if r['day']==case['day'] and r['symbol']!=case['symbol']
        and r['symbol'] not in excluded and (r['liquidity_bin'],r['age_bin'])==(case['liquidity_bin'],case['age_bin'])]
    if failures_only:
        eligible=[r for r in eligible if not r['delayed']['180d_2x']['hit']]
    eligible.sort(key=lambda r:(abs(math.log(r['liquidity']/case['liquidity']))+
                               abs(math.log(r['history_days']/case['history_days'])),r['symbol']))
    return eligible[:3]


def wilson(successes,n):
    if not n:
        return None
    if not 0<=successes<=n:
        raise ValueError('invalid_success_count')
    z=1.959963984540054; p=successes/n; den=1+z*z/n
    center=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0,center-radius),min(1,center+radius)]


def summarize(pairs):
    out={}
    for era in [*ERAS,'all']:
        rows=[r for r in pairs if era=='all' or era_of(r['case']['day'])==era]
        by_label={}
        for label in LABELS:
            n=len(rows);hits=sum(r['case']['delayed'][label]['hit'] for r in rows)
            matched=[r for r in rows if r['controls']]
            deltas=[int(r['case']['delayed'][label]['hit'])-
                    statistics.fmean(int(c['delayed'][label]['hit']) for c in r['controls']) for r in matched]
            tokens=len({r['case']['symbol'] for r in rows})
            by_label[label]=dict(events=n,hits=hits,success_fraction=hits/n if n else None,
                false_discovery_fraction=1-hits/n if n else None,wilson95_binomial_reference=wilson(hits,n),
                matched_events=len(matched),matched_controls=sum(len(r['controls']) for r in rows),
                control_success_fraction=statistics.fmean(statistics.fmean(int(c['delayed'][label]['hit']) for c in r['controls']) for r in matched) if matched else None,
                paired_success_difference=statistics.fmean(deltas) if deltas else None,
                token_clusters=tokens,cluster_interval=None,
                interval_reason='FEWER_THAN_10_TOKEN_CLUSTERS' if tokens<10 else 'NOT_A_CONFIRMATORY_STUDY')
            if len(deltas)>=2:
                rng=random.Random(20261025)
                boots=sorted(statistics.fmean(rng.choices(deltas,k=len(deltas))) for _ in range(2000))
                # This is a sensitivity diagnostic, explicitly not valid clustered inference.
                by_label[label]['event_bootstrap95_descriptive_only']=[boots[49],boots[1949]]
        out[era]=by_label
    return out
