"""Frozen account uncertainty, concentration, cashflow and decision arithmetic."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_trading.historical_data import write_json
from one_trading.repricing_atlas import quantile
from one_trading.repricing_v3 import STRATEGIES, ERAS
from scripts.validate_cashflow_alpha import recompute, block_bootstrap


def paired_interval(a, b, repetitions=2000):
    keys = sorted(a)
    if keys != sorted(b):
        raise AssertionError('unpaired_months')
    values = [a[k]['return']-b[k]['return'] for k in keys]
    rng = random.Random(20261025)
    estimates = []
    for _ in range(repetitions):
        sampled = []
        while len(sampled) < len(values):
            start = rng.randrange(len(values))
            sampled.extend(values[(start+i) % len(values)] for i in range(6))
        estimates.append(statistics.fmean(sampled[:len(values)]))
    return dict(mean_monthly_advantage=statistics.fmean(values),
                interval_95pct=[quantile(estimates,.025), quantile(estimates,.975)],
                interval_97_5pct=[quantile(estimates,.0125), quantile(estimates,.9875)],
                n_months=len(keys), block_months=6, repetitions=repetitions)


def trade_uncertainty(campaigns):
    groups = defaultdict(list)
    for t in campaigns:
        d = datetime.fromtimestamp(t['opened_ms']/1000, timezone.utc)
        groups[(d.year,(d.month-1)//6)].append(t['pnl'])
    groups = list(groups.values())
    rng = random.Random(20261025)
    ev, pf = [], []
    for _ in range(2000 if len(groups)>=2 else 0):
        values = [p for _ in groups for p in rng.choice(groups)]
        ev.append(statistics.fmean(values))
        loss = -sum(p for p in values if p<0)
        if loss:
            pf.append(sum(p for p in values if p>0)/loss)
    return dict(entry_halfyear_clusters=len(groups),
                expectancy_95pct=[quantile(ev,.025),quantile(ev,.975)] if ev else None,
                pf_95pct=[quantile(pf,.025),quantile(pf,.975)] if len(pf)>=1900 else None)


def details(result):
    independent = recompute(result)
    campaigns = independent['campaigns']
    pnls = [t['pnl'] for t in campaigns]
    current = longest = 0
    for t in sorted(campaigns, key=lambda t:(t['closed_ms'],t['symbol'])):
        current = current+1 if t['pnl']<0 else 0
        longest = max(longest, current)
    wins = sorted((v for v in pnls if v>0), reverse=True)
    by_symbol = defaultdict(float)
    realized = defaultdict(float)
    for t in campaigns:
        by_symbol[t['symbol']] += t['pnl']
    for f in result['fills']:
        if f['side']=='SELL':
            key=datetime.fromtimestamp(f['ts_ms']/1000,timezone.utc).strftime('%Y-%m')
            realized[key] += f['realized_pnl']
    for w in result['writeoffs']:
        key=datetime.fromtimestamp(w['ts_ms']/1000,timezone.utc).strftime('%Y-%m')
        realized[key] -= w['cost_basis_lost']
    monthly = []
    initial = result['config']['initial_cash']
    for month, r in result['monthly'].items():
        monthly.append(dict(month=month, return_fraction=r['return'],
            fixed20m_krw=r['return']*20_000_000,
            compounded_account_krw=(r['final_equity']-r['initial_equity'])*20_000_000/initial,
            realized_krw=realized[month]*20_000_000/initial,
            exposure=r['average_exposure']))
    targets = {}
    for method in ('fixed20m_krw','compounded_account_krw','realized_krw'):
        targets[method] = {str(target):sum(r[method]>=target for r in monthly) for target in (5_000_000,10_000_000)}
    n=len(pnls)
    mean=statistics.fmean(pnls) if pnls else 0.
    variance=statistics.fmean((v-mean)**2 for v in pnls) if pnls else 0.
    return dict(monthly=monthly, targets=targets, full_months=len(monthly),
                longest_losing_campaign_streak=longest,
                max_close_open_risk=max(r['open_risk_fraction'] for r in result['equity']),
                top5_winners_fraction_of_gross_profit=sum(wins[:5])/sum(wins) if wins else None,
                pnl_skewness=statistics.fmean((v-mean)**3 for v in pnls)/variance**1.5 if variance else None,
                return_ex_top5_winners=(sum(pnls)-sum(wins[:5]))/initial,
                top_symbol_fraction_of_gross_profit=max(by_symbol.values())/sum(wins) if wins else None,
                # Gross accounting bridge holds fills constant; zero-cost rerun
                # separately measures sizing/behavior changes under costs.
                same_fills_before_cost_return=(sum(pnls)+result['summary']['fees_paid']+result['summary']['slippage_cost'])/initial,
                bootstrap=block_bootstrap(independent['metrics']['monthly_returns'],repetitions=2000,block_length=6),
                trade_uncertainty=trade_uncertainty(campaigns))


def analyze(directory, audit_passed):
    directory=Path(directory)
    all_details={}
    results={}
    for path in sorted(directory.glob('*__*__*.json')):
        r=json.loads(path.read_text(encoding='utf-8'))
        all_details[path.stem]=details(r)
        results[path.stem]=r
    paired={}
    decisions={}
    for strategy in ('demand','compression'):
        paired[strategy]={}
        research={}
        for fold in ('continuous',*ERAS):
            r=results[f'{strategy}__stress__{fold}']
            control=results[f'price_control__stress__{fold}']
            paired[strategy][fold]=paired_interval(r['monthly'],control['monthly'])
            if fold in ('validation','challenge'):
                s=r['summary']
                research.update({f'{fold}_positive_return':s['total_return']>0,
                    f'{fold}_pf_gt1_30':s['profit_factor'] is not None and s['profit_factor']>1.30,
                    f'{fold}_positive_expectancy':s['expectancy_per_closed_trade'] is not None and s['expectancy_per_closed_trade']>0,
                    f'{fold}_paired_lower_positive':paired[strategy][fold]['interval_97_5pct'][0]>0,
                    f'{fold}_sample_ge30':s['trade_count']>=30})
        continuous=results[f'{strategy}__stress__continuous']
        det=all_details[f'{strategy}__stress__continuous']
        stress=[results[f'{strategy}__stress__{fold}']['summary'] for fold in ('continuous',*ERAS)]
        live=dict(research_edge=all(research.values()), audit_passed=audit_passed,
            no_halt=all(not s['latched_drawdown_halt'] for s in stress),
            max_drawdown=all(s['max_drawdown']>=-.15 for s in stress),
            positive_month_fraction=continuous['summary']['positive_month_fraction']>=.55,
            mean_monthly_ge1pct=statistics.fmean(x['return'] for x in continuous['monthly'].values())>=.01,
            target5m_frequency_ge50pct=det['targets']['fixed20m_krw']['5000000']/det['full_months']>=.5)
        measured=[k for k,v in research.items() if not v and not k.endswith('sample_ge30')]
        research_decision='REJECT' if measured else 'RESEARCH_EDGE' if all(research.values()) and audit_passed else 'NEEDS_MORE_EVIDENCE'
        live_measured=[k for k,v in live.items() if not v and k not in ('audit_passed','research_edge')]
        decision='REJECT' if measured or live_measured else 'FORWARD_PAPER_CANDIDATE' if all(live.values()) else 'NEEDS_MORE_EVIDENCE'
        decisions[strategy]=dict(research_decision=research_decision, decision=decision,
            research_gates=research, live_relevance_gates=live, actual_live_eligible=False,
            failed_research_gates=[k for k,v in research.items() if not v],
            failed_relevance_gates=[k for k,v in live.items() if not v])
    payload=dict(accounts=all_details, paired_controls=paired, decisions=decisions)
    write_json(directory/'account_analysis.json',payload)
    return payload


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True,type=Path)
    a=p.parse_args()
    audit=json.loads((a.run/'independent_review.json').read_text(encoding='utf-8'))
    print(json.dumps(analyze(a.run,audit['passed'])['decisions'],indent=2))
