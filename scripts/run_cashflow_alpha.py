#!/usr/bin/env python3
"""Frozen, offline Cashflow Alpha experiment; never submits exchange orders."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_trading.historical_data import load_histories, ms, verify_derived, write_json
from one_trading.replay import verify_archive
from one_trading.walkforward import HistoricalPanel


def file_sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def experiment(archive, output, start, end):
    from one_trading.cashflow_alpha import default_config, run
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    protocol_path = ROOT / 'docs/CASHFLOW_ALPHA_PROTOCOL.json'
    protocol = json.loads(protocol_path.read_text())
    config = default_config()
    manifest = {
        'protocol': protocol,
        'protocol_sha256': file_sha(protocol_path),
        'config': config,
        'run_started_utc': datetime.now(timezone.utc).isoformat(),
        'git_head_at_run': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'code_sha256': {str(p.relative_to(ROOT)): file_sha(p) for p in
                        sorted((ROOT/'one_trading').glob('*.py')) + [Path(__file__)]},
        'archive_sha256': file_sha(archive),
        'start': start, 'end_exclusive': end,
        'live_orders': False,
    }
    write_json(out/'protocol.json', manifest)
    print(json.dumps({'phase': 'protocol_frozen', 'protocol_sha256': manifest['protocol_sha256']}), flush=True)
    evidence = verify_archive(Path(archive))
    evidence.update(verify_derived(archive))
    histories, coverage = load_histories(archive)
    write_json(out/'dataset.json', {'verification': evidence, 'coverage': coverage})
    panel = HistoricalPanel(histories, config)
    a, z = ms(start), ms(end)
    first_year = datetime.fromtimestamp(a/1000, timezone.utc).year
    last_year = datetime.fromtimestamp((z-1)/1000, timezone.utc).year
    folds = [('continuous', a, z)] + [(str(y), max(a, ms(f'{y}-01-01')), min(z, ms(f'{y+1}-01-01')))
                                    for y in range(first_year, last_year+1)]
    results = {}
    for case, costs in [('base', {'fee_rate': .001, 'slippage_rate': .001}),
                        ('stress', {'fee_rate': .002, 'slippage_rate': .003})]:
        results[case] = {}
        case_config = {**config, **costs}
        for name, fold_start, fold_end in folds:
            t = time.monotonic()
            result = run(panel, case_config, fold_start, fold_end, missing='zero')
            write_json(out/f'{case}_{name}.json', result)
            results[case][name] = result['summary']
            print(json.dumps({'phase': 'fold_complete', 'case': case, 'fold': name,
                              'seconds': round(time.monotonic()-t, 2),
                              'summary': result['summary']}), flush=True)
    write_json(out/'comparison.json', results)
    write_json(out/'artifact_hashes.json', {p.name: file_sha(p) for p in sorted(out.glob('*.json'))})
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--start', default='2019-01-01')
    p.add_argument('--end', default='2026-10-01', help='exclusive UTC boundary')
    a = p.parse_args()
    experiment(a.archive, a.output, a.start, a.end)


if __name__ == '__main__':
    main()
