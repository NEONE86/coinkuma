"""Separate source parser, no import of the primary supplemental evidence auditor."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
import zipfile


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify(root, prior, run):
    manifest = read(root/'manifest.json')
    tickers = {(r['symbol'],r['day']):r for r in read(run/'ticker_samples.json')}
    raws = {(r['symbol'],r['day'],r['offset']):r for r in read(run/'raw_oi_samples.json')}
    row_count = oi_count = 0
    for meta in manifest:
        key = hashlib.sha256(meta.get('cache_identity',meta['url']).encode()).hexdigest()
        body = (root/'http'/(key+'.bin')).read_bytes()
        assert hashlib.sha256(body).hexdigest()==meta['sha256'] and len(body)==meta['bytes']
        if meta['status']!=200 or meta['kind']=='documentation':
            continue
        if meta['kind']=='derivative_ticker':
            fields = gzip.decompress(body).decode('utf-8-sig').splitlines()
            names = fields[0].split(','); data = list(csv.reader(fields[1:]))
            col = {n:i for i,n in enumerate(names)}
            n = positive = changed = same = negative = inversions = 0
            prev_oi = prev_local = None; stamps = []
            start = int(datetime.fromisoformat(meta['day']).replace(tzinfo=timezone.utc).timestamp())*1_000_000
            for values in data:
                n+=1
                assert values[col['symbol']]==meta['symbol'] and values[col['exchange']]=='binance-futures'
                t = int(values[col['timestamp']]); l = int(values[col['local_timestamp']]); stamps.append(l)
                assert start<=l<start+86_400_000_000
                negative += l<t; inversions += prev_local is not None and l<prev_local; prev_local=l
                v = values[col['open_interest']]
                if v:
                    number=Decimal(v); assert number.is_finite() and number>=0; positive+=1
                    if prev_oi is not None:
                        changed+=number!=prev_oi; same+=number==prev_oi
                    prev_oi=number
            actual=dict(rows=n,nonempty_oi_rows=positive,observed_oi_value_changes=changed,
                repeated_oi_value_rows=same,negative_timestamp_lag_rows=negative,local_out_of_order_rows=inversions,
                first_local_us=min(stamps),last_local_us=max(stamps))
            report=tickers[(meta['symbol'],meta['day'])]
            assert all(report[k]==v for k,v in actual.items()),actual
            row_count+=n
        else:
            events=[];lags=[];disconnects=0
            start=int(datetime.fromisoformat(meta['day']).replace(tzinfo=timezone.utc).timestamp())*1_000_000+meta['offset']*60_000_000
            for line in gzip.decompress(body).decode().splitlines():
                if not line:
                    disconnects+=1;continue
                pos=line.index('{'); message=json.loads(line[pos:]); local=line[:pos].strip()
                # Independent integer parser retains microseconds without float conversion.
                seconds=int(datetime.strptime(local[:19],'%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
                fraction=local[20:-1];us=seconds*1_000_000+int((fraction+'000000')[:6])
                assert start<=us<start+60_000_000
                d=message['data'];assert message['generated'] and message['stream']==meta['symbol'].lower()+'@openInterest'
                assert d['symbol']==meta['symbol'] and Decimal(d['openInterest']).is_finite() and Decimal(d['openInterest'])>=0
                event=int(d['time']);events.append(event);lags.append(us-event*1000)
            report=raws[(meta['symbol'],meta['day'],meta['offset'])]
            actual=dict(rows=len(events),disconnects=disconnects,negative_lag_rows=sum(x<0 for x in lags),
                min_lag_us=min(lags),max_lag_us=max(lags),earliest_event_ms=min(events),latest_event_ms=max(events))
            assert all(report[k]==v for k,v in actual.items()),actual
            oi_count+=len(events)
    funding=read(root/'connector_probes.json'); original=read(prior/'file_audits.json')
    published={(r['symbol'],r['day']):r for r in read(run/'funding_samples.json')}
    total=positive=0
    for request in funding['requests']:
        assert request['status']=='fulfilled'; v=request['value']; p=v['params']
        result=v['result'];assert not result.get('isError'); rows=result['structuredContent']['result']
        meta=next(f for f in original if f['symbol']==p['symbol'] and f['stream']=='fundingRate' and f['period']==v['day'][:7])
        body=(prior/'http'/(hashlib.sha256(meta['url'].encode()).hexdigest()+'.bin')).read_bytes()
        assert hashlib.sha256(body).hexdigest()==meta['sha256']
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            data=list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode('utf-8-sig'))))
        expected={int(r[0]):Decimal(r[2]) for r in data if r[0]!='calc_time' and p['startTime']<=int(r[0])<=p['endTime']}
        actual={int(r['fundingTime']):Decimal(r['fundingRate']) for r in rows}
        assert len(actual)==len(rows) and actual==expected and all(r['symbol']==p['symbol'] for r in rows)
        marks=[Decimal(r['markPrice']) for r in rows if r['markPrice']]
        assert all(m.is_finite() and m>0 for m in marks)
        report=published[(p['symbol'],v['day'])]
        assert report['rows']==len(rows) and report['positive_settlement_marks']==len(marks)
        assert report['missing_settlement_marks']==len(rows)-len(marks) and report['exact_archive_event_rate_matches']==len(rows)
        total+=len(rows);positive+=len(marks)
    decision=read(run/'decision.json')
    required=(datetime(2026,10,1)-datetime(2022,1,1)).days
    days={s:sorted({d for symbol,d in tickers if symbol==s}) for s in ('BTCUSDT','ETHUSDT')}
    assert required==decision['required_calendar_days_per_symbol'] and days==decision['sampled_ticker_days']
    assert all(len(d)<required for d in days.values()) and positive<total
    expected_reasons={s+':'+r for s in days for r in ('OI_CONTINUOUS_HISTORICAL_RECEIPT_INCOMPLETE','EXACT_SETTLEMENT_MARK_COVERAGE_INCOMPLETE')}
    expected_reasons.update(('EFFECTIVE_DATED_CONTRACT_TERMS_INCOMPLETE','STRUCTURE_HISTORICAL_AVAILABILITY_INCOMPLETE'))
    assert set(decision['reasons'])==expected_reasons
    assert decision['decision']=='NEEDS_MORE_EVIDENCE' and not decision['data_gate_passed']
    assert not decision['strategy_backtest_executed'] and decision['performance_metrics'] is None
    for name,digest in read(run/'run_manifest.json')['private_source_hashes'].items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
    result=dict(independent_data_reproduction_passed=True,external_third_party_audit=False,
        scope='SOURCE_BYTES_AND_SUPPLEMENTAL_EVIDENCE_ONLY_NOT_STRATEGY_PERFORMANCE',sources_verified=len(manifest),
        ticker_rows=row_count,raw_OI_rows=oi_count,funding_event_rate_matches=total,positive_settlement_marks=positive,
        missing_settlement_marks=total-positive,decision='NEEDS_MORE_EVIDENCE',strategy_performance_reproduced=False,
        terms_extraction_independently_authenticated=False)
    (run/'independent.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--prior-archive',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);a=p.parse_args();verify(a.archive,a.prior_archive,a.run)
