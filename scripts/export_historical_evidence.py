#!/usr/bin/env python3
"""Export reviewed aggregate results only; no prices, trades or model records."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


def load(p): return json.loads(Path(p).read_text())
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def pct(v):return f'{100*v:.2f}%'
def day(t):return datetime.fromtimestamp(t/1000,timezone.utc).date().isoformat()


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--collection',required=True)
    p.add_argument('--reference',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();run=Path(a.run);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
    results=load(run/'comparison.json');dataset=load(run/'dataset.json');protocol=load(run/'protocol.json')
    collection=load(a.collection);reference=load(Path(a.reference)/'reference_collection.json')
    cross=load(Path(a.reference)/'crossvenue_comparison.json')
    write(out/'comparison.json',results);write(out/'protocol.json',protocol)
    write(out/'dataset_summary.json',{'archive':dataset['archive'],
        'collection':{k:v for k,v in collection.items() if k!='coverage'},
        'reference':reference,'crossvenue_comparison':cross})
    with (out/'coverage.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['symbol','daily_bars','first_daily_bucket_UTC','last_daily_bucket_UTC','internal_missing_days'])
        for r in collection['coverage']:w.writerow([r['symbol'],r['bars'],day(r['first_ms']),day(r['last_ms']),r['missing_days']])
    base=results['base']['continuous'];stress=results['stress']['continuous']
    lines=['# ONE real-data walk-forward results — 2026-10-17','',
      'New-data historical experiment. **Not reproduction of the missing original base/stress runs.**',
      'No parameter search or performance-driven tuning was conducted in this run.','',
      f"Data: {collection['markets']} Binance USDT spot histories, {collection['daily_bars']:,} daily candles, "
      f"{collection['planned_zip_count']:,} monthly source ZIPs. Collection: 2018-01-01 through 2026-09-30 UTC.",
      f"Shortened daily bars retained: {collection['shortened_daily_bars']}; corrupt source rows quarantined: {collection['rejected_candles']}; histories with internal gaps: {collection['markets_with_gaps']}.",
      'Bitstamp: five fixed major USD pairs, 15,975 reference OHLCV bars; not a complete historical universe.',
      'Source bytes, publisher checksums, normalized-row regeneration and SQLite integrity were verified before replay.','',
      '## Continuous account: 2019-01-01 through 2026-09-30 UTC','',
      '| Case | Strategy net return | Strategy MDD | BTC net return | BTC MDD | Fees | Slippage | Fills | Avg exposure | Halt latched |',
      '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for case in ['base','stress']:
        r=results[case]['continuous'];lines.append(f"| {case} | {pct(r['total_return'])} | {pct(r['max_drawdown'])} | {pct(r['btc']['total_return'])} | {pct(r['btc']['max_drawdown'])} | {r['fees_paid']:.2f} | {r['slippage_cost']:.2f} | {r['fill_count']} | {pct(r['average_daily_exposure'])} | {r['latched_drawdown_halt']} |")
    lines += ['', 'Initial virtual capital: 10,000 USDT. MDD is daily-close marked-equity drawdown. Fees/slippage amounts are USDT. Base = 10/10 bps each side; stress = 20/30 bps each side. Cost cases retrain/re-execute; they are not the same trades with a fee subtracted.', '',
      '## Independent yearly folds','',
      'Each year starts with fresh virtual capital/risk state and preceding-data model warmup. Do not compound this table into a supposed continuous investable result.','',
      '| Year | Base return | Base MDD | Base BTC return | Stress return | Stress MDD | Base fills | Base avg exposure |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for year in [k for k in results['base'] if k!='continuous']:
        b=results['base'][year];s=results['stress'][year]
        lines.append(f"| {year} | {pct(b['total_return'])} | {pct(b['max_drawdown'])} | {pct(b['btc']['total_return'])} | {pct(s['total_return'])} | {pct(s['max_drawdown'])} | {b['fill_count']} | {pct(b['average_daily_exposure'])} |")
    lines += ['', '2026 ends on September 30. Full metrics, including each cost case’s BTC benchmark and costs, are in `comparison.json`.','',
      '## Prior-day BTC regime attribution (continuous base account)','',
      '| Regime | Days | Strategy return on classified days | BTC return on classified days |','|---|---:|---:|---:|']
    for k,r in base['regime'].items():lines.append(f"| {k} | {r['days']} | {pct(r['strategy_compounded_selected_days'])} | {pct(r['btc_compounded_selected_days'])} |")
    lines += ['', 'Regimes use the prior close versus SMA200 and 28-day momentum. Returns compound disjoint daily observations; they are attribution, not separately executable strategies.','',
      '## Interpretation and limitations','',
      '- The recovered price-only policy is evaluated under a new historical-liquidity universe and documented missing-market assumptions. The original strategy/broker thresholds were retained.',
      '- This run does not establish profitable autonomous trading, whole-market coverage, or untouched out-of-sample alpha. Archive availability, token identity, trade feasibility and liquidity assumptions remain limitations.',
      '- Full fundamental revaluation analysis is absent: usage, revenue, token value capture, unlocks, publication-timed catalysts and revisions are not connected.',
      '- Listing coverage consists of observed daily-bucket boundaries plus a small curated official-notice seed. These are not an exhaustive cross-exchange listing/delisting register.',
      '- Missing held markets use an explicitly pessimistic full-loss writeoff with no fabricated sale. See writeoff counts in the machine-readable comparison and the workflow for the alternative fail mode.',
      '- Two file-backed dataset builds failed SQLite integrity checks and were rejected before performance calculation. The final archive was rebuilt from frozen source bytes in memory and atomically exported; the underlying filesystem cause was not established.',
      '- All original and new test results must be distinguished from these real-market-data simulation outputs. No real orders were sent.', '',
      '## Reproduce and audit','',
      'See [workflow](../../HISTORICAL_WORKFLOW.md), `protocol.json`, `dataset_summary.json`, `coverage.csv` and `comparison.json`.',
      f"Normalized candle SHA-256: `{collection['normalized_candles_sha256']}`.",
      f"Source-manifest SHA-256: `{collection['source_manifest_sha256']}`.",
      'Frozen raw bytes and full runtime artifacts are retained separately. Collected prices and individual fills/model records are deliberately not committed to this repository.','']
    (out/'RESULTS.md').write_text('\n'.join(lines))
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file()}
    write(out/'SHA256.json',hashes)

if __name__=='__main__':main()
