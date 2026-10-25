"""Acquire public documented evidence samples; never account or order data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    'https://docs.tardis.dev/api/http-api-reference.md',
    'https://docs.tardis.dev/historical-data-details/binance-futures.md',
    'https://docs.tardis.dev/downloadable-csv-files/data-types.md',
    'https://docs.tardis.dev/api/instruments-metadata-api.md',
    'https://docs.tardis.dev/downloadable-csv-files.md',
    'https://raw.githubusercontent.com/tardis-dev/tardis-node/master/src/mappers/binance.ts',
    'https://www.binance.com/en/support/announcement/detail/aa735cd2d8bd4e7bb092179cf086e486',
    'https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d',
]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def collect(out):
    plan = json.loads((ROOT/'docs/FUTURES_EVIDENCE_EXTENSION_PLAN.json').read_text())
    cache = out/'http'
    cache.mkdir(parents=True, exist_ok=True)
    tasks = [dict(kind='documentation', url=url) for url in DOCS]
    for symbol in plan['symbols']:
        for day in plan['tardis_sample_days']:
            path = day.replace('-', '/')
            tasks.append(dict(kind='derivative_ticker', symbol=symbol, day=day,
                url=f'https://datasets.tardis.dev/v1/binance-futures/derivative_ticker/{path}/{symbol}.csv.gz'))
            for offset in plan['tardis_raw_minute_offsets']:
                filters = json.dumps([dict(channel='openInterest', symbols=[symbol.lower()])], separators=(',', ':'))
                query = urllib.parse.urlencode({'from': day, 'filters': filters, 'offset': offset})
                tasks.append(dict(kind='raw_open_interest', symbol=symbol, day=day, offset=offset,
                    url='https://api.tardis.dev/v1/data-feeds/binance-futures?'+query))

    def fetch(task):
        # Preserve the initial 406 responses; protocol-correct retry has its own identity.
        compressed = task['kind'] == 'raw_open_interest'
        identity = task['url'] + ('|Accept-Encoding:gzip' if compressed else '')
        stem = cache/sha(identity.encode())
        meta, body = stem.with_suffix('.json'), stem.with_suffix('.bin')
        if meta.exists():
            value = json.loads(meta.read_text())
            if value['url'] != task['url'] or sha(body.read_bytes()) != value['sha256']:
                raise ValueError('cache_integrity_failure')
            return value
        started = datetime.now(timezone.utc).isoformat()
        headers = {'User-Agent':'ONE-evidence-audit/1.0'}
        if compressed:
            headers['Accept-Encoding'] = 'gzip'
        request = urllib.request.Request(task['url'], headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=45)
        except urllib.error.HTTPError as e:
            response = e
        except (urllib.error.URLError, TimeoutError) as e:
            value = dict(**task, cache_identity=identity, started_utc=started, finished_utc=datetime.now(timezone.utc).isoformat(),
                status=None, transport_error=type(e).__name__, sha256=sha(b''), bytes=0)
            body.write_bytes(b''); write(meta, value)
            return value
        with response:
            raw = response.read(80_000_001)
            if len(raw)>80_000_000:
                raise ValueError('response_size_limit')
            value = dict(**task, cache_identity=identity, started_utc=started, finished_utc=datetime.now(timezone.utc).isoformat(),
                status=response.status, final_url=response.geturl(), content_type=response.headers.get('Content-Type'),
                content_encoding=response.headers.get('Content-Encoding'), last_modified=response.headers.get('Last-Modified'),
                sha256=sha(raw), bytes=len(raw))
        body.write_bytes(raw); write(meta, value)
        print(json.dumps({k:value.get(k) for k in ('kind','symbol','day','offset','status','bytes')}), flush=True)
        return value

    with ThreadPoolExecutor(max_workers=4) as pool:
        manifest = list(pool.map(fetch, tasks))
    write(out/'manifest.json', manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    collect(parser.parse_args().output)
