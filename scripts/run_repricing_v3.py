"""Freeze, audit and execute v3. All outputs are local, offline research."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_trading.historical_data import load_histories, ms, write_json
from one_trading.repricing_v3 import AtlasPanel, STRATEGIES, ERAS, END, config_for, run
from one_trading.repricing_atlas import make_atlas, analyze_rates
from scripts.cashflow_data_audit import audit
from scripts.validate_cashflow_alpha import recompute, check_reported_summary


def file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def code_hashes():
    files = [p for directory in ('one_trading', 'scripts', 'tests') for p in (ROOT/directory).glob('*.py')]
    files += [ROOT/'config.json', ROOT/'docs/REPRICING_V3_PROTOCOL.json']
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() for p in sorted(files)}


def freeze(path):
    if path.exists():
        raise ValueError('freeze_exists')
    write_json(path, dict(frozen_utc=datetime.now(timezone.utc).isoformat(),
        protocol_commit='affc281', parent='e49f3f06518eb3bf4ea59e82680f96c46acf2828',
        head_before_code_freeze=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        code_sha256=code_hashes(), v3_outcomes_inspected=False, empirical_parameter_trials=0))


def validate_freeze(path):
    f = json.loads(path.read_text(encoding='utf-8'))
    if f['code_sha256'] != code_hashes():
        raise ValueError('frozen_code_changed')
    relative = path.resolve().relative_to(ROOT).as_posix()
    committed = subprocess.check_output(['git','show','HEAD:'+relative], cwd=ROOT)
    if json.loads(committed) != f:
        raise ValueError('freeze_must_be_committed')
    subprocess.run(['git','merge-base','--is-ancestor', f['parent'], 'HEAD'], cwd=ROOT, check=True)
    return f


def experiment(archive, out, frozen_path):
    frozen = validate_freeze(frozen_path)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out/'run_manifest.json', dict(
        started_utc=datetime.now(timezone.utc).isoformat(),
        frozen_commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        archive_sha256=file_sha(archive), freeze=frozen, live_orders=False))
    data = audit(archive, out/'source_audit')
    if not data['matches_pr2_normalized_candles'] or data['collection']['markets'] != 756:
        raise ValueError('unexpected_dataset')
    h, _ = load_histories(archive)
    print(json.dumps(dict(phase='SOURCE_VERIFIED', markets=len(h), bars=sum(map(len,h.values())))), flush=True)
    panel = AtlasPanel(h, config_for('price_control')).prepare()
    summary, anchors, episodes, matches = make_atlas(panel, out)
    write_json(out/'atlas_summary.json', summary)
    write_json(out/'atlas_statistics.json', analyze_rates(anchors, episodes, matches))
    del anchors, episodes, matches
    spec = json.loads((ROOT/'docs/REPRICING_V3_PROTOCOL.json').read_text(encoding='utf-8'))
    comparison = {}
    for strategy in STRATEGIES:
        comparison[strategy] = {}
        cases = {**spec['costs'], 'zero':dict(fee_rate=0., slippage_rate=0.),
                 'delay3_stress':spec['costs']['stress']}
        for case,costs in cases.items():
            c = config_for(strategy, costs, 3 if case == 'delay3_stress' else 2)
            folds = {'continuous':(ms('2019-01-01'), END)}
            if case in ('base', 'stress'):
                folds.update(ERAS)
            comparison[strategy][case] = {}
            for fold,(a,z) in folds.items():
                result = run(panel, c, a, z)
                independent = recompute(result)
                check_reported_summary(result, independent)
                write_json(out/f'{strategy}__{case}__{fold}.json', result)
                comparison[strategy][case][fold] = result['summary']
                print(json.dumps(dict(phase='ACCOUNT_COMPLETE', strategy=strategy, case=case, fold=fold,
                                      total_return=result['summary']['total_return'],
                                      trades=result['summary']['trade_count'])), flush=True)
        write_json(out/'comparison.json', comparison)
    write_json(out/'artifact_hashes.json', {p.relative_to(out).as_posix():file_sha(p) for p in sorted(out.rglob('*')) if p.is_file()})
    print(json.dumps(dict(phase='EXPERIMENT_COMPLETE')), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--freeze', type=Path, required=True)
    p.add_argument('--freeze-only', action='store_true')
    p.add_argument('--archive', type=Path)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    if a.freeze_only:
        freeze(a.freeze)
    else:
        if not a.archive or not a.output:
            p.error('--archive and --output required')
        experiment(a.archive, a.output, a.freeze)


if __name__ == '__main__':
    main()
