"""Audit supplemental evidence without weakening the frozen futures protocol."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

DAY = 86_400_000


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def milliseconds(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()*1000)


def source_bytes(root, meta):
    identity = meta.get('cache_identity', meta['url'])
    raw = (root/'http'/(sha(identity.encode())+'.bin')).read_bytes()
    if sha(raw) != meta['sha256'] or len(raw) != meta['bytes']:
        raise ValueError('source_integrity_failure')
    return raw


def ticker_audit(raw, symbol, day):
    a = milliseconds(day)*1000; z = a+DAY*1000
    count = valid = oi_changes = unchanged = negative_lag = out_of_order = 0
    previous_oi = previous_local = None
    first = last = None
    for row in csv.DictReader(io.StringIO(gzip.decompress(raw).decode('utf-8-sig'))):
        count += 1
        event, local = int(row['timestamp']), int(row['local_timestamp'])
        if row['exchange'] != 'binance-futures' or row['symbol'] != symbol or not a <= local < z:
            raise ValueError('ticker_identity_or_day')
        if previous_local is not None and local < previous_local:
            out_of_order += 1
        previous_local = local
        negative_lag += local < event
        first = local if first is None else min(first,local); last = local if last is None else max(last,local)
        if row['open_interest']:
            oi = Decimal(row['open_interest'])
            if not oi.is_finite() or oi < 0:
                raise ValueError('invalid_ticker_OI')
            valid += 1
            if previous_oi is not None:
                oi_changes += oi != previous_oi
                unchanged += oi == previous_oi
            previous_oi = oi
    return dict(symbol=symbol,day=day,rows=count,nonempty_oi_rows=valid,observed_oi_value_changes=oi_changes,
        repeated_oi_value_rows=unchanged,negative_timestamp_lag_rows=negative_lag,
        local_out_of_order_rows=out_of_order,first_local_us=first,last_local_us=last,
        fresh_OI_event_timestamps_recoverable_from_csv_alone=False)


def raw_oi_audit(raw, symbol, day, offset):
    lo = milliseconds(day)+offset*60_000
    events = []; lags = []; disconnects = 0
    # The prefix may have seven subsecond digits; retain source bytes, use integer
    # microseconds conservatively for diagnostics, never a strategy execution time.
    for line in gzip.decompress(raw).decode().splitlines():
        if not line:
            disconnects += 1; continue
        prefix, payload = line.split(' ',1)
        message = json.loads(payload); data = message['data']
        local = datetime.fromisoformat(prefix.replace('Z','+00:00'))
        local_us = int(local.timestamp())*1_000_000+local.microsecond
        if message['stream'] != symbol.lower()+'@openInterest' or message.get('generated') is not True or data['symbol'] != symbol:
            raise ValueError('raw_OI_identity')
        oi = Decimal(data['openInterest']); event = int(data['time'])
        if not oi.is_finite() or oi < 0 or not lo*1000 <= local_us < (lo+60_000)*1000:
            raise ValueError('raw_OI_value_or_slice')
        events.append(event); lags.append(local_us-event*1000)
    return dict(symbol=symbol,day=day,offset=offset,rows=len(events),disconnects=disconnects,
        negative_lag_rows=sum(x<0 for x in lags),min_lag_us=min(lags) if lags else None,
        max_lag_us=max(lags) if lags else None,earliest_event_ms=min(events) if events else None,
        latest_event_ms=max(events) if events else None,
        evidence='PROVIDER_REPORTED_HISTORICAL_RECEIPT_SAMPLE_NOT_FULL_INTERVAL')


def funding_audit(root, prior_archive):
    capture = load(root/'connector_probes.json'); files = load(prior_archive/'file_audits.json')
    result = []
    for item in capture['requests']:
        if item['status'] != 'fulfilled':
            raise ValueError('connector_failure')
        item = item['value']; params = item['params']; day = item['day']; symbol = params['symbol']
        response = item['result']
        if response.get('isError'):
            raise ValueError('connector_error')
        rows = response['structuredContent']['result']
        f = next(f for f in files if f['symbol']==symbol and f['stream']=='fundingRate' and f['period']==day[:7])
        body = (prior_archive/'http'/(sha(f['url'].encode())+'.bin')).read_bytes()
        if sha(body) != f['sha256']:
            raise ValueError('prior_archive_tampered')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            records = list(csv.reader(io.StringIO(archive.read(archive.namelist()[0]).decode('utf-8-sig'))))
        expected = {int(r[0]):Decimal(r[2]) for r in records[1:] if params['startTime']<=int(r[0])<=params['endTime']}
        received = {}; marks = 0
        for row in rows:
            stamp = int(row['fundingTime'])
            if row['symbol']!=symbol or stamp in received:
                raise ValueError('funding_symbol_or_duplicate')
            received[stamp] = Decimal(row['fundingRate'])
            value = row.get('markPrice')
            if value:
                number = Decimal(value)
                if not number.is_finite() or number<=0:
                    raise ValueError('invalid_settlement_mark')
                marks += 1
        if received != expected:
            raise ValueError('funding_exact_time_rate_mismatch')
        result.append(dict(symbol=symbol,day=day,rows=len(rows),positive_settlement_marks=marks,
            missing_settlement_marks=len(rows)-marks,exact_archive_event_rate_matches=len(expected),
            api_url='https://fapi.binance.com/fapi/v1/fundingRate',params=params,
            started_utc=item['started_utc'],finished_utc=item['finished_utc'],
            source_format='NORMALIZED_MCP_NOT_ORIGINAL_HTTP',archive_zip_sha256=f['sha256']))
    return result


def gate(plan, tickers, raw_samples, funding):
    required_days = (milliseconds(plan['evaluation'][1])-milliseconds(plan['evaluation'][0]))//DAY
    # Samples establish possible sources, not continuous provenance or signal eligibility.
    sampled = {s:sorted({r['day'] for r in tickers if r['symbol']==s}) for s in plan['symbols']}
    reasons = []
    for symbol in plan['symbols']:
        if len(sampled[symbol]) < required_days:
            reasons.append(symbol+':OI_CONTINUOUS_HISTORICAL_RECEIPT_INCOMPLETE')
        if not any(r['symbol']==symbol and r['rows'] and not r['negative_lag_rows'] for r in raw_samples):
            reasons.append(symbol+':RAW_OI_RECEIPT_UNVERIFIED')
        # Full settlement eligibility cannot be inferred from any number of isolated days.
        days = {r['day'] for r in funding if r['symbol']==symbol and r['rows'] and not r['missing_settlement_marks']}
        if len(days) < required_days:
            reasons.append(symbol+':EXACT_SETTLEMENT_MARK_COVERAGE_INCOMPLETE')
    # Neither complete historical terms nor continuous structure receipt sources
    # are produced by this sampler. A new evidence adapter is needed to qualify them.
    reasons.append('EFFECTIVE_DATED_CONTRACT_TERMS_INCOMPLETE')
    reasons.append('STRUCTURE_HISTORICAL_AVAILABILITY_INCOMPLETE')
    # This supplemental sampler intentionally cannot certify a full future engine.
    return dict(decision='NEEDS_MORE_EVIDENCE',data_gate_passed=False,reasons=reasons,
        required_calendar_days_per_symbol=required_days,sampled_ticker_days=sampled,
        sample_source_progress=True,continuous_qualified_signal_observations=0,
        strategy_backtest_executed=False,performance_metrics=None,
        strategy_performance_independent_reproduction=False,orders=0,leverage_applied=False)


def run(root, prior_archive, out, repo):
    manifest = load(root/'manifest.json'); plan = load(repo/'docs/FUTURES_EVIDENCE_EXTENSION_PLAN.json')
    frozen = load(repo/'docs/experiments/futures-4h-2026-10-25/freeze.json')
    if any(sha((repo/p).read_bytes().replace(b'\r\n',b'\n'))!=h for p,h in frozen['code_sha256'].items()):
        raise ValueError('PR5_frozen_policy_or_code_changed')
    plan_path = 'docs/FUTURES_EVIDENCE_EXTENSION_PLAN.json'
    committed = subprocess.check_output(['git','show','HEAD:'+plan_path],cwd=repo)
    if json.loads(committed)!=plan:
        raise ValueError('evidence_plan_must_be_committed')
    subprocess.run(['git','merge-base','--is-ancestor',plan['parent_pr5_head'],'HEAD'],cwd=repo,check=True)
    ticker = []; raw = []
    for meta in manifest:
        body = source_bytes(root,meta)
        if meta['status'] != 200:
            continue
        if meta['kind']=='derivative_ticker':
            ticker.append(ticker_audit(body,meta['symbol'],meta['day']))
        elif meta['kind']=='raw_open_interest':
            raw.append(raw_oi_audit(body,meta['symbol'],meta['day'],meta['offset']))
    funding = funding_audit(root,prior_archive)
    decision = gate(plan,ticker,raw,funding)
    out.mkdir(parents=True,exist_ok=False)
    reports = dict(sources=manifest,ticker_samples=ticker,raw_oi_samples=raw,funding_samples=funding,decision=decision)
    provenance = {'connector_probes.json':sha((root/'connector_probes.json').read_bytes()),
        'terms_web_extract.txt':sha((root/'terms_web_extract.txt').read_bytes())}
    reports['run_manifest'] = dict(parent_pr5_head=plan['parent_pr5_head'],private_source_hashes=provenance,
        plan_sha256=sha((repo/'docs/FUTURES_EVIDENCE_EXTENSION_PLAN.json').read_bytes().replace(b'\r\n',b'\n')),
        unchanged_protocol_sha256=sha((repo/'docs/FUTURES_4H_PROTOCOL.json').read_bytes().replace(b'\r\n',b'\n')))
    for name,value in reports.items():
        (out/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(decision=decision,ticker_rows=sum(x['rows'] for x in ticker),raw_OI_rows=sum(x['rows'] for x in raw),funding_marks=sum(x['positive_settlement_marks'] for x in funding))))
