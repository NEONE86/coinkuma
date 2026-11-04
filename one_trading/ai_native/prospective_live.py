"""Finite live-input PAPER pilot; source capture stays in a separate process.

Default model is a zero-cost deterministic connection probe, NOT intelligence.
Every run exclusively creates an outside-repository directory; no auto-resume
or automatic retry of an uncertain external model invocation.
"""
import argparse
import json
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .engine import Experiment, Protocol, ModelDecision, DecisionSkipped, canonical, instant, sha256
from .planners import CashPlanner, MomentumPlanner, MODEL_INSTRUCTION
from .prospective_evidence import EvidenceRecorder
from .live_model import FreeProbe, ResponsesModel


def now():
    return datetime.now(timezone.utc)


def write_new(path, value):
    with Path(path).open('xb') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def consume_approval(approval):
    """One durable claim per approval reference, independent of runtime path.

    Failed launches do not refund this claim. A new attempt requires a new
    approval; changing parents or concurrently launching cannot reset budget.
    """
    state = Path.home()/'.one-trading-approval-consumption'
    state.mkdir(mode=0o700, exist_ok=True)
    reference = approval['approval_reference']
    write_new(state/(sha256(reference)+'.json'), {
        'approval_sha256': sha256(approval), 'claimed_at': now().isoformat(),
        'run_id': approval['run_id'], 'source_commit': approval['source_commit']})


class Tail:
    def __init__(self, path):
        self.path, self.offset = Path(path), 0

    def read(self):
        if not self.path.exists():
            return []
        rows = []
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline()
                if not line.endswith(b'\n'):
                    break
                rows.append(json.loads(line))
                self.offset = stream.tell()
        return rows


class LivePlanner:
    def __init__(self, recorder, model, root, start, config):
        self.recorder, self.model, self.root = recorder, model, root
        self.start, self.config = start, config
        self.calls, self.next_call, self.failed = 0, 0., False
        self.enabled = False

    def select_epoch(self, frame):
        elapsed = time.monotonic()-self.start
        age = (now()-instant(frame['received_at'])).total_seconds()
        if hasattr(self.model, 'approval') and instant(self.model.approval['expires_at']) <= now():
            self.failed = True
        self.enabled = (not self.failed and self.calls < self.config['max_calls']
            and elapsed >= self.next_call and 0 <= age <= 4
            and elapsed < self.config['duration_seconds']-35
            and self.recorder.spent['AI']+Decimal(self.model.reserve) <= Decimal(self.config['cost_limit_usdt']))

    def propose(self, context):
        elapsed = time.monotonic()-self.start
        age = (now()-instant(context['cutoff'])).total_seconds()
        if not self.enabled:
            return DecisionSkipped()
        if not 0 <= age <= 5:
            return None
        if (self.recorder.spent['AI'] + Decimal(self.model.reserve) >
                Decimal(self.config['cost_limit_usdt'])):
            return None
        rid = 'request-'+str(self.calls+1)
        # Persist original full model input BEFORE external dispatch.
        write_new(self.root/(rid+'-input.json'), context)
        self.recorder.request(request_id=rid, frame_id=context['frame_id'], policy_id='AI',
            reserve_cost_usdt=self.model.reserve, deadline_seconds=30, at=now(),
            elapsed=time.monotonic()-self.start)
        self.calls += 1
        self.next_call = elapsed+self.config['decision_interval_seconds']
        try:
            answer, receipt = self.model.respond(context)
            received = now()
            write_new(self.root/(rid+'-provider.json'), receipt)
            self.recorder.complete(request_id=rid, at=received, elapsed=time.monotonic()-self.start,
                actual_cost_usdt=receipt['cost'], response_bytes=answer,
                input_tokens=receipt['input_tokens'], output_tokens=receipt['output_tokens'])
            self.recorder.decide(rid, at=now(), elapsed=time.monotonic()-self.start)
            event = json.loads((self.root/"decisions.jsonl").read_bytes().splitlines()[-1])
            ready = instant(event['at'])
            accepted = event['kind'] == 'proposal_validated'
            if Decimal(receipt['cost']) > Decimal(self.model.reserve):
                self.failed = True
                accepted = False
            return ModelDecision(json.loads(answer) if accepted else None,
                (ready-instant(context['cutoff'])).total_seconds(), float(receipt['cost']),
                event['payload']['valid_until'] if accepted else None)
        except Exception as exc:
            # Preserve unknown completion as unresolved; no fabricated zero bill.
            self.failed = True
            write_new(self.root/(rid+'-failure.json'), {'error_type': type(exc).__name__,
                'at': now().isoformat(), 'status': 'UNRESOLVED_NO_RETRY'})
            return None


class ScheduledControl:
    def __init__(self, planner, schedule):
        self.planner, self.schedule = planner, schedule

    def propose(self, context):
        if not self.schedule.enabled:
            return DecisionSkipped()
        return self.planner.propose(context)


def run(root, config, *, enable_public_network=False, approval=None):
    root = Path(root).resolve()
    repo = Path(__file__).resolve().parents[2]
    if root.is_relative_to(repo) or root.exists():
        raise ValueError('NEW_PRIVATE_OUTSIDE_REPO_DIRECTORY_REQUIRED')
    if not enable_public_network:
        raise ValueError('EXPLICIT_PUBLIC_NETWORK_REQUIRED')
    if (type(config['duration_seconds']) is not int or not 60 <= config['duration_seconds'] <= 3600
            or type(config['max_calls']) is not int or not 1 <= config['max_calls'] <= 100
            or not 10 <= config['decision_interval_seconds'] <= 600
            or type(config['max_bytes']) is not int or not 100000 <= config['max_bytes'] <= 100000000):
        raise ValueError('PILOT_BOUNDS')
    git = ['git', '-c', 'safe.directory='+str(repo), '-C', str(repo)]
    head = subprocess.check_output(git+['rev-parse', 'HEAD'], text=True).strip()
    if head != config['source_commit'] or subprocess.check_output(git+['status','--porcelain','--untracked-files=normal'], text=True).strip():
        raise ValueError('EXACT_CLEAN_SOURCE_REQUIRED')
    files = subprocess.check_output(git+['ls-files'], text=True).splitlines()
    source_hashes = {f: hashlib.sha256((repo/f).read_bytes()).hexdigest() for f in files}
    model = ResponsesModel(approval) if approval else FreeProbe()
    if approval:
        if (approval['source_commit'] != config['source_commit'] or
                approval['run_id'] != root.name or approval['max_calls'] != config['max_calls'] or
                approval['cost_limit_usdt'] != config['cost_limit_usdt'] or
                any(approval[k] != config[k] for k in ('duration_seconds','max_bytes','decision_interval_seconds')) or
                instant(approval['expires_at']) <= now()):
            raise ValueError('APPROVAL_IDENTITY_MISMATCH')
        consume_approval(approval)
    elif config['cost_limit_usdt'] != '0':
        raise ValueError('FREE_PROBE_REQUIRES_ZERO_BUDGET')
    root.mkdir(mode=0o700)
    write_new(root/"source-manifest.json", {"commit": head, "files": source_hashes, "sha256": sha256(source_hashes)})
    write_new(root/'config.json', dict(config, model=model.model,
        mode='LIVE_MODEL' if approval else 'FREE_PROBE_NOT_AI', approval=approval,
        run_id=root.name, prompt_sha256=sha256(MODEL_INSTRUCTION)))
    registry = {name: dict(version=version, model=None, prompt_sha256=None,
                          call_limit=0, cost_limit_usdt='0')
                for name, version in [('CASH', 'cash-v1'), ('PRICE_ONLY', 'momentum-v1')]}
    registry['AI'] = dict(version='live-v1', model=model.model,
        prompt_sha256=sha256(MODEL_INSTRUCTION), call_limit=config['max_calls'],
        cost_limit_usdt=config['cost_limit_usdt'])
    start = time.monotonic()
    rec = EvidenceRecorder(root/'decisions.jsonl', run_id=root.name, source_class='LIVE_PUBLIC',
        collector_commit=config['source_commit'], registry=registry, start_at=now(),
        model_calls_authorized=True)
    planner = LivePlanner(rec, model, root, start, config)
    engine = Experiment(Protocol(), {'CASH': ScheduledControl(CashPlanner(), planner), 'PRICE_ONLY': ScheduledControl(MomentumPlanner(), planner),
                                    'AI': planner}, data_use='PROSPECTIVE_REPLAY')
    capture = root/'source'
    argv = [sys.executable, '-B', '-X', 'utf8', '-m', 'one_trading.ai_native.bybit_forward',
            '--output-dir', str(capture), '--duration-seconds', str(config['duration_seconds']),
            '--max-bytes', str(config['max_bytes']), '--enable-public-network']
    write_new(root/'launch.json', {'argv': argv, 'at': now().isoformat(), 'automatic_restart': False})
    tails = {n: Tail(capture/(n+'.jsonl')) for n in ('raw', 'events', 'frames')}
    raw, prov, queue = {}, {}, []
    failure = None
    persisted = 0
    exited = False
    with (root/'collector.stdout.log').open('wb') as out, (root/'collector.stderr.log').open('wb') as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            with (root/'paper-events.jsonl').open('xb') as journal:
                while True:
                    for item in tails['raw'].read():
                        raw[item['seq']] = {k: item[k] for k in ('seq', 'sha256', 'received_at')}
                    for event in tails['events'].read():
                        if event['kind'] == 'frame_provenance':
                            prov[event['frame_id']] = event
                    queue.extend(tails['frames'].read())
                    while queue and queue[0]['id'] in prov:
                        frame = queue[0]; p = prov[frame['id']]
                        if not all(seq in raw for seq in p['source_seqs'].values()):
                            break
                        queue.pop(0)
                        rec.freeze(frame, source_receipts={s: raw[n] for s,n in p['source_seqs'].items()},
                            raw_cutoff_seq=p['raw_cutoff_seq'], memory=[], at=now(), elapsed=time.monotonic()-start)
                        planner.select_epoch(frame)
                        engine.observe(frame, source_received_at={s: raw[n]['received_at'] for s,n in p['source_seqs'].items()})
                        for row in engine.ledger.records[persisted:]:
                            journal.write(canonical(row)+b'\n')
                        journal.flush(); os.fsync(journal.fileno())
                        persisted = len(engine.ledger.records)
                    if proc.poll() is not None:
                        if exited:
                            if queue:
                                raise ValueError('UNBOUND_FINAL_FRAME')
                            break
                        exited = True
                    time.sleep(.05)
        except BaseException as exc:
            failure = type(exc).__name__+':'+str(exc)
            # No restart, source files preserved. Stop only this child on failure.
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        finally:
            rec.close(at=now(), elapsed=time.monotonic()-start)
    write_new(root/'termination.json', {'collector_returncode': proc.returncode, 'error': failure,
                                       'at': now().isoformat(), 'model_uncertain': planner.failed})
    if engine._frames >= 2:
        write_new(root/'paper.json', engine.finish())
    from .live_audit import verify
    try:
        result = verify(root)
    except Exception as exc:
        result = {'status': 'FAIL_INDEPENDENT_AUDIT', 'error': type(exc).__name__+':'+str(exc),
                  'model_calls_external': planner.calls if approval else 0,
                  'economic_alpha': 'NOT_ESTABLISHED'}
    write_new(root/'audit.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--source-commit', required=True)
    p.add_argument('--duration-seconds', type=int, default=90)
    p.add_argument('--max-bytes', type=int, default=10000000)
    p.add_argument('--max-calls', type=int, default=3)
    p.add_argument('--decision-interval-seconds', type=int, default=15)
    p.add_argument('--cost-limit-usdt', default='0')
    p.add_argument('--approval', type=Path)
    p.add_argument('--enable-public-network', action='store_true')
    args = p.parse_args()
    config = {k: v for k,v in vars(args).items() if k not in ('output_dir', 'approval', 'enable_public_network')}
    result = run(args.output_dir, config, enable_public_network=args.enable_public_network,
                 approval=json.loads(args.approval.read_text()) if args.approval else None)
    print(json.dumps(result))
    if not result['status'].startswith('PASS_'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
