"""Ex-post labels and matched research controls. Never imported by the planner."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import random
import statistics

from .observer import DAY
from .repricing_v3 import ERAS, END, STRATEGIES, era_of, mature_in_era
from .walkforward import IDENTITY_BOUNDARIES


def day_string(day):
    return datetime.fromtimestamp(day/1000, timezone.utc).strftime('%Y-%m-%d')


def path_labels(panel, symbol, first, reference, stop):
    """180 future calendar days; zero recovery at first gap/identity break."""
    low = reference
    max_high = reference
    hit = {}
    stopped = False
    ended = None
    last_close = reference
    out = {}
    for offset in range(180):
        day = first+offset*DAY
        b = panel.bar(symbol, day)
        if ended is None:
            if b is None or day in IDENTITY_BOUNDARIES.get(symbol, []):
                ended = 'IDENTITY_BOUNDARY' if day in IDENTITY_BOUNDARIES.get(symbol, []) else 'MISSING_OR_ENDED'
                low = 0.
                last_close = 0.
                stopped = True
            else:
                low = min(low, b.low)
                max_high = max(max_high, b.high)
                stopped = stopped or b.low <= stop
                last_close = b.close
                for multiple in (2, 3):
                    if multiple not in hit and b.high >= multiple*reference:
                        hit[multiple] = dict(hit=True, days=offset+1,
                                             mae=low/reference-1,
                                             stop_before_or_same_bar=stopped)
        if offset+1 in (90, 180):
            horizon = offset+1
            for multiple in (2, 3):
                out[f'{horizon}d_{multiple}x'] = hit.get(multiple, dict(
                    hit=False, days=None, mae=low/reference-1, stop_before_or_same_bar=stopped)).copy()
            out[f'{horizon}d_end_return'] = last_close/reference-1
            out[f'{horizon}d_max_return'] = max_high/reference-1
            out[f'{horizon}d_status'] = ended or 'OBSERVED_THROUGH_HORIZON'
    return out


def labels(panel, symbol, day):
    b = panel.bar(symbol, day)
    research = path_labels(panel, symbol, day+DAY, b.close, .85*b.close)
    entry = panel.bar(symbol, day+2*DAY)
    # Include missing entry as an explicit unexecutable failure, never drop it.
    executable = entry is not None and day+2*DAY not in IDENTITY_BOUNDARIES.get(symbol, [])
    tradable = path_labels(panel, symbol, day+2*DAY, entry.open if executable else b.close, .85*b.close)
    if not executable:
        for horizon in (90, 180):
            for mult in (2, 3):
                tradable[f'{horizon}d_{mult}x'] = dict(hit=False, days=None, mae=-1., stop_before_or_same_bar=True)
            tradable[f'{horizon}d_end_return'] = -1.
            tradable[f'{horizon}d_max_return'] = 0.
            tradable[f'{horizon}d_status'] = 'NO_ENTRY_BAR_OR_IDENTITY'
    return dict(research=research, delayed=tradable, entry_available=executable,
                entry_gap=entry.open/b.close-1 if executable else None,
                broker_gap_acceptable=executable and .90*b.close <= entry.open <= 1.03*b.close,
                final_history_status='ENDS_BEFORE_DATASET_END' if panel.h[symbol][-1].open_ms < END-DAY else 'OBSERVED_AT_DATASET_END',
                final_observed_day=panel.h[symbol][-1].open_ms)


def make_atlas(panel, output):
    out = Path(output)
    counts = Counter()
    aggregates = defaultdict(Counter)
    markets = defaultdict(Counter)
    regime_counts = defaultdict(Counter)
    anchors, sample, episodes = [], [], []
    last_anchor = {}
    # Successful anchor controls need the full same-date eligible panel. Keep
    # only Sunday cohorts temporarily, not the entire daily label panel in RAM.
    matches, match_coverage = [], Counter()
    with gzip.open(out/'OPPORTUNITY_ATLAS.jsonl.gz', 'wt', encoding='utf-8', newline='\n') as f:
        for b in panel.h['BTCUSDT']:
            day = b.open_ms
            era = era_of(day)
            if era is None:
                continue
            if day+181*DAY >= END:
                counts['immature_eligible_symbol_dates_excluded'] += len(panel.universe(day))
                continue
            sunday = (day//DAY+3) % 7 == 6
            cohort = []
            for symbol in panel.universe(day):
                r = panel.features(symbol, day)
                r.update(labels(panel, symbol, day))
                r['era'] = era
                r['boundary_purged'] = not mature_in_era(day)
                counts['eligible_daily_rows'] += 1
                counts['boundary_purged_daily_rows'] += r['boundary_purged']
                market = markets[symbol]
                market['eligible_daily_rows'] += 1
                market['ends_before_dataset_end'] = int(r['final_history_status'] == 'ENDS_BEFORE_DATASET_END')
                for horizon in (90, 180):
                    for mult in (2, 3):
                        k = f'{horizon}d_{mult}x'
                        market[k+'_hits'] += r['research'][k]['hit']
                        for scope in (era, 'all'):
                            a = aggregates[(scope, k)]
                            a['n'] += 1
                            a['hits'] += r['research'][k]['hit']
                            a['delayed_hits'] += r['delayed'][k]['hit']
                            a['stop_preceded_delayed_hit'] += r['delayed'][k]['hit'] and r['delayed'][k]['stop_before_or_same_bar']
                            a['ended_paths'] += r['research'][f'{horizon}d_status'] != 'OBSERVED_THROUGH_HORIZON'
                f.write(json.dumps(r, separators=(',', ':'), allow_nan=False)+'\n')
                if sunday and not r['boundary_purged']:
                    cohort.append(r)
                    key = (symbol, era)
                    if day-last_anchor.get(key, -10**18) >= 182*DAY:
                        anchors.append(r)
                        last_anchor[key] = day
                        for label in ('90d_2x','90d_3x','180d_2x','180d_3x'):
                            rc=regime_counts[(era,r['regime'],label)]
                            rc['n']+=1
                            rc['hits']+=r['research'][label]['hit']
                    for strategy in STRATEGIES:
                        if symbol in panel.episodes[strategy].get(day, set()):
                            episodes.append(dict(strategy=strategy, **r))
                # Deterministic audit selection, independent of label values.
                if counts['eligible_daily_rows'] % 997 == 0:
                    sample.append(r)
            if cohort:
                cases = [r for r in anchors if r['day'] == day and r['research']['180d_2x']['hit']]
                failures = [r for r in cohort if not r['research']['180d_2x']['hit']]
                used = set()
                for case in sorted(cases, key=lambda r:r['symbol']):
                    candidates = [r for r in failures if r['symbol'] not in used and
                                  (r['liquidity_bin'], r['age_bin']) == (case['liquidity_bin'], case['age_bin'])]
                    candidates.sort(key=lambda r:(abs(math.log(r['liquidity']/case['liquidity']))+
                                                  abs(math.log(r['history_days']/case['history_days'])), r['symbol']))
                    selected = candidates[:3]
                    match_coverage['cases'] += 1
                    match_coverage['matched_cases'] += bool(selected)
                    match_coverage['control_rows'] += len(selected)
                    used.update(r['symbol'] for r in selected)
                    matches.append(dict(case=case, controls=selected))
            if day_string(day).endswith('-01-01'):
                print(json.dumps(dict(phase='ATLAS_YEAR', date=day_string(day), rows=counts['eligible_daily_rows'])), flush=True)
    for name, values in [('inference_anchors', anchors), ('signal_episodes', episodes),
                         ('atlas_audit_sample', sample), ('MATCHED_CONTROLS', matches)]:
        with gzip.open(out/(name+'.json.gz'), 'wt', encoding='utf-8') as f:
            json.dump(values, f, separators=(',', ':'), allow_nan=False)
    summary = dict(counts=counts, anchors=len(anchors), episodes=len(episodes),
                   matching=match_coverage,
                   markets=[dict(symbol=s, **v) for s,v in sorted(markets.items())],
                   regime_anchors=[dict(era=e,regime=g,label=k,**v) for (e,g,k),v in sorted(regime_counts.items())],
                   daily_descriptive=[dict(era=e, label=k, **v) for (e,k),v in sorted(aggregates.items())])
    return summary, anchors, episodes, matches


def quantile(values, p):
    if not values:
        return None
    v = sorted(values)
    x = (len(v)-1)*p
    lo = int(x)
    return v[lo]+(v[min(lo+1, len(v)-1)]-v[lo])*(x-lo)


def slice_flags(r):
    return {'all': True, 'volume_ge_1_5': r['volume_ratio'] >= 1.5,
            'relative28_positive': r['relative28'] > 0, 'tr_le_0_7': r['tr_ratio'] <= .7,
            'btc_above_sma200': r['btc_above'], **{s:r[s] for s in STRATEGIES}}


def cluster_key(r, kind):
    if kind == 'symbol':
        return r['symbol']
    d = datetime.fromtimestamp(r['day']/1000, timezone.utc)
    return (d.year, (d.month-1)//6)


def rate_statistics(rows, feature, label, repetitions=2000):
    # n, successes, signals, signal-successes, failures, false-positives
    def vector(r):
        y = int(r['research'][label]['hit'])
        s = int(slice_flags(r)[feature])
        return (1, y, s, s*y, 1-y, s*(1-y))
    def stats(t):
        n,y,s,sy,fail,fp = t
        return [y/n if n else None, sy/s if s else None,
                fp/fail if fail else None, fp/s if s else None,
                sy/s-y/n if s and n else None]
    total = [sum(v) for v in zip(*(vector(r) for r in rows))] if rows else [0]*6
    point = stats(total)
    intervals = []
    cluster_counts = {}
    for kind in ('calendar_halfyear', 'symbol'):
        groups = defaultdict(lambda: [0]*6)
        for r in rows:
            a = groups[cluster_key(r, kind)]
            for j,v in enumerate(vector(r)):
                a[j] += v
        groups = list(groups.values())
        cluster_counts[kind] = len(groups)
        rng = random.Random(f'20261025:{feature}:{label}:{kind}')
        samples = [[] for _ in range(5)]
        for _ in range(repetitions if len(groups) >= 2 else 0):
            sums = [0]*6
            for _ in groups:
                g = rng.choice(groups)
                for j in range(6):
                    sums[j] += g[j]
            for j, v in enumerate(stats(sums)):
                if v is not None:
                    samples[j].append(v)
        intervals.append([[quantile(v, .025), quantile(v, .975)] if len(v) >= repetitions*.95 else None for v in samples])
    names = ['base_probability', 'conditional_probability', 'false_positive_rate', 'false_discovery_rate', 'lift_difference']
    result = dict(n=total[0], successes=total[1], signals=total[2], signal_successes=total[3],
                  clusters=cluster_counts)
    for j,name in enumerate(names):
        bounds = [x[j] for x in intervals if x[j] is not None]
        result[name] = point[j]
        result[name+'_95pct'] = [min(x[0] for x in bounds), max(x[1] for x in bounds)] if len(bounds)==2 else None
    return result


def analyze_rates(anchors, episodes, matches):
    out = []
    for era in ERAS:
        rows = [r for r in anchors if r['era'] == era]
        for label in ('90d_2x', '90d_3x', '180d_2x', '180d_3x'):
            for feature in slice_flags(rows[0]) if rows else []:
                out.append(dict(era=era, label=label, feature=feature,
                                **rate_statistics(rows, feature, label)))
        print(json.dumps(dict(phase='RATES_DONE', era=era)), flush=True)
    episode_summary = []
    for era in ERAS:
        for strategy in STRATEGIES:
            rows = [r for r in episodes if r['era']==era and r['strategy']==strategy]
            for label in ('90d_2x', '90d_3x', '180d_2x', '180d_3x'):
                successful = [r for r in rows if r['research'][label]['hit']]
                delayed = [r for r in rows if r['delayed'][label]['hit']]
                episode_summary.append(dict(era=era, strategy=strategy, label=label,
                    n=len(rows), hits=len(successful), delayed_hits=len(delayed),
                    delayed_stop_preceded=sum(r['delayed'][label]['stop_before_or_same_bar'] for r in delayed),
                    median_days=statistics.median(r['research'][label]['days'] for r in successful) if successful else None,
                    median_mae=statistics.median(r['research'][label]['mae'] for r in successful) if successful else None,
                    no_entry=sum(not r['entry_available'] for r in rows),
                    gap_rejected=sum(not r['broker_gap_acceptable'] for r in rows)))
    matched = []
    for era in ERAS:
        pairs = [p for p in matches if p['case']['era']==era and p['controls']]
        for feature in ('volume_ratio', 'relative28', 'relative90', 'tr_ratio', 'liquidity', 'history_days'):
            differences = [p['case'][feature]-statistics.fmean(r[feature] for r in p['controls']) for p in pairs]
            matched.append(dict(era=era, feature=feature, cases=len(pairs),
                                median_case_minus_mean_control=statistics.median(differences) if differences else None))
    return dict(rates=out, episode_summary=episode_summary, matched_differences=matched)
