"""Freeze and execute exactly the two PR3 challengers; offline simulation only."""
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
from one_trading.historical_data import load_histories, ms, write_json
from one_trading.cashflow_alpha_v2 import STRATEGIES, ChallengerPanel, default_config, run
from scripts.validate_cashflow_alpha import recompute


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def code_hashes():
    paths = [p for d in ('one_trading', 'scripts', 'tests') for p in (ROOT/d).glob('*.py')]
    paths += [ROOT/'config.json', ROOT/'docs/CASHFLOW_ALPHA_NEXT_EXPERIMENT.json',
              ROOT/'docs/CASHFLOW_ALPHA_V2_PROTOCOL.json']
    # Git normalizes text to LF. Hash the same canonical bytes on Windows/Linux;
    # binary archive/artifact hashes continue to use their exact original bytes.
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            for p in sorted(paths)}


def freeze(path):
    if Path(path).exists():
        raise ValueError('freeze_exists_do_not_overwrite')
    value = dict(frozen_utc=datetime.now(timezone.utc).isoformat(),
                 parent_pr3='e6a78766aeb4462fc4aeb01f80fcbf24ca7096cb',
                 head_before_freeze_commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
                 source_spec=json.loads((ROOT/'docs/CASHFLOW_ALPHA_NEXT_EXPERIMENT.json').read_text(encoding='utf-8')),
                 conventions=json.loads((ROOT/'docs/CASHFLOW_ALPHA_V2_PROTOCOL.json').read_text(encoding='utf-8')),
                 code_sha256=code_hashes(), results_inspected=False)
    write_json(path, value)
    print(json.dumps(dict(phase='FROZEN_NOT_RUN', files=len(value['code_sha256'])), ensure_ascii=True), flush=True)


def validate_freeze(path):
    f = json.loads(Path(path).read_text(encoding='utf-8'))
    if code_hashes() != f['code_sha256']:
        raise ValueError('frozen_code_changed_new_experiment_required')
    subprocess.run(['git', 'merge-base', '--is-ancestor', f['parent_pr3'], 'HEAD'], cwd=ROOT, check=True)
    relative = Path(path).resolve().relative_to(ROOT).as_posix()
    committed = subprocess.check_output(['git','show',f'HEAD:{relative}'], cwd=ROOT)
    if json.loads(committed.decode('utf-8')) != f:
        raise ValueError('freeze_must_be_committed_before_results')
    return f


def experiment(archive, output, frozen_path):
    frozen = validate_freeze(frozen_path)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    head = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
    write_json(out/'run_manifest.json', dict(freeze_sha256=sha(frozen_path), frozen_commit=head,
               run_started_utc=datetime.now(timezone.utc).isoformat(), archive_sha256=sha(archive),
               code_sha256=code_hashes(), live_orders=False))
    # All source bytes, CHECKSUM files and derived prices independently checked
    # before the first account. Raw details remain in ignored local directories.
    from scripts.cashflow_data_audit import audit
    data = audit(Path(archive), out/'data_audit')
    histories, _ = load_histories(archive)
    print(json.dumps(dict(phase='DATA_VERIFIED', candles=data['collection']['daily_bars'],
                         normalized=data['collection']['normalized_candles_sha256'])), flush=True)
    spec = frozen['conventions']
    start, end = ms(spec['evaluation_start']), ms(spec['evaluation_end_exclusive'])
    folds = [('continuous', start, end)]
    folds += [(str(y), ms(f'{y}-01-01'), min(end,ms(f'{y+1}-01-01'))) for y in range(2019, 2027)]
    folds += [(name, ms(a), ms(z)) for name,(a,z) in spec['era_accounts'].items()]
    comparison = {}
    for strategy in STRATEGIES:
        config = default_config(strategy)
        panel = ChallengerPanel(histories, config)
        comparison[strategy] = {}
        for case, costs in spec['costs'].items():
            c = {**config, **costs}
            comparison[strategy][case] = {}
            for name, a, z in folds:
                t = time.monotonic()
                result = run(panel, c, a, z)
                path = out/f'{strategy}__{case}__{name}.json'
                write_json(path, result)
                comparison[strategy][case][name] = result['summary']
                print(json.dumps(dict(phase='ACCOUNT_DONE',strategy=strategy,case=case,fold=name,
                                     seconds=round(time.monotonic()-t,2),summary=result['summary'])), flush=True)
                del result
            write_json(out/'comparison.json', comparison)
        # Stress-only controls are specified before results, 200 per challenger.
        c = {**config, **spec['costs']['stress']}
        controls = []
        controls_dir = out/f'{strategy}__controls'
        controls_dir.mkdir()
        for seed in range(200):
            result = run(panel, c, start, end, control_seed=seed)
            independent = recompute(result)
            means = list(independent['metrics']['monthly_returns'].values())
            retained = dict(seed=seed, summary=result['summary'], independent=independent,
                            mean_monthly_net_return=sum(means)/len(means),
                            ledger_sha256=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest())
            # Private complete ledger permits later review of every control.
            write_json(controls_dir/f'{seed:03d}.json', result)
            write_json(controls_dir/f'{seed:03d}_audit.json', retained)
            controls.append({k:v for k,v in retained.items() if k != 'independent'} | {'ledger_checks':independent['checks'], 'monthly_returns':independent['metrics']['monthly_returns']})
            del result, independent, retained
            if (seed+1)%10 == 0:
                write_json(out/f'{strategy}__controls.json', controls)
                print(json.dumps(dict(phase='CONTROLS_DONE',strategy=strategy,count=seed+1)), flush=True)
        write_json(out/f'{strategy}__controls.json', controls)
        del panel
    write_json(out/'artifact_hashes.json', {p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*.json'))})
    print(json.dumps(dict(phase='EXPERIMENT_COMPLETE', frozen_commit=head)), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--freeze', required=True)
    p.add_argument('--freeze-only', action='store_true')
    p.add_argument('--archive')
    p.add_argument('--output')
    a = p.parse_args()
    if a.freeze_only:
        freeze(a.freeze)
    else:
        if not a.archive or not a.output:
            p.error('--archive and --output required for execution')
        experiment(a.archive, a.output, a.freeze)


if __name__ == '__main__':
    main()
