"""Public archive ingestion; original bytes and derived rows are separate evidence.

No keys, accounts, orders, host rotation or synthetic market observations.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from .observer import Bar, DAY, NoRedirect, Record, Store, json_text

INDEX = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
BULK = 'https://data.binance.vision/'
NS = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, snapshot_id INTEGER NOT NULL,
 sha256 TEXT NOT NULL, fetched_ms INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candles(
 venue TEXT NOT NULL, symbol TEXT NOT NULL, open_ms INTEGER NOT NULL,
 close_ms INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL,
 c REAL NOT NULL, volume REAL NOT NULL, quote_volume REAL,
 source_id INTEGER NOT NULL, PRIMARY KEY(venue,symbol,open_ms));
CREATE TABLE IF NOT EXISTS market_events(
 venue TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
 effective_ms INTEGER NOT NULL, published_ms INTEGER, collected_ms INTEGER NOT NULL,
 source_url TEXT NOT NULL, evidence_level TEXT NOT NULL,
 PRIMARY KEY(venue,symbol,kind,effective_ms,source_url));
CREATE TABLE IF NOT EXISTS rejected_candles(
 venue TEXT NOT NULL, symbol TEXT NOT NULL, source_id INTEGER NOT NULL,
 row_sha256 TEXT NOT NULL, raw_row TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(source_id,row_sha256));
CREATE INDEX IF NOT EXISTS candle_day ON candles(venue,open_ms);
CREATE INDEX IF NOT EXISTS candle_source ON candles(source_id);
'''


def ms(date):
    dt=datetime.fromisoformat(date)
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()*1000)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    temp.replace(path)


class Cache:
    """Content-checked frozen HTTP cache. Resume does not silently refresh data."""
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.local = threading.local()

    def download(self, url):
        """Reuse TLS connections; respect the configured network proxy unchanged."""
        target=urllib.parse.urlsplit(url)
        if target.scheme!='https' or target.netloc not in {
            'data.binance.vision','s3-ap-northeast-1.amazonaws.com',
            'www.bitstamp.net','www.binance.com'}:
            raise ValueError('unapproved_bulk_source')
        proxy=urllib.parse.urlsplit(urllib.request.getproxies().get('https',''))
        if proxy.scheme not in {'','http'} or proxy.username:
            # Let urllib handle uncommon authenticated/TLS proxy configurations.
            req=urllib.request.Request(url,headers={'User-Agent':'ONE-historical-research/1.0'})
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as r:
                return r.read(20_000_001)
        if not hasattr(self.local,'connections'):self.local.connections={}
        key=target.netloc
        if key not in self.local.connections:
            if proxy.scheme=='http':
                conn=http.client.HTTPSConnection(proxy.hostname,proxy.port,timeout=30)
                conn.set_tunnel(target.hostname,443)
            else:conn=http.client.HTTPSConnection(target.hostname,443,timeout=30)
            self.local.connections[key]=conn
        conn=self.local.connections[key]
        try:
            conn.request('GET',urllib.parse.quote(target.path,safe='/%')+('?' + target.query if target.query else ''),
                         headers={'User-Agent':'ONE-historical-research/1.0'})
            response=conn.getresponse();raw=response.read(20_000_001)
            if response.status!=200:
                raise urllib.error.HTTPError(url,response.status,response.reason,response.headers,None)
            if len(raw)>20_000_000:raise ValueError('response_too_large')
            return raw
        except Exception:
            conn.close();self.local.connections.pop(key,None)
            raise

    def get(self, url):
        stem = self.root / sha(url.encode())
        meta, body = stem.with_suffix('.json'), stem.with_suffix('.bin')
        if meta.exists():
            m = json.loads(meta.read_text()); raw = body.read_bytes()
            if m['url'] != url or sha(raw) != m['sha256']:
                raise ValueError('cache_integrity_failure:'+url)
            return raw, m
        if self.stop.is_set():
            raise RuntimeError('source_stopped_after_access_or_rate_denial')
        try:
            raw=self.download(url)
            if len(raw)>20_000_000:raise ValueError('response_too_large')
        except urllib.error.HTTPError as exc:
            if exc.code in {401,403,407,418,429,451}: self.stop.set()
            raise
        m = dict(url=url, sha256=sha(raw), fetched_ms=int(time.time()*1000), bytes=len(raw))
        body.write_bytes(raw); write_json(meta,m)
        return raw,m

    def listing(self, prefix, delimiter=None):
        marker = None; entries = []; evidence = []
        while True:
            params = {'prefix':prefix, 'max-keys':1000}
            if delimiter: params['delimiter']=delimiter
            if marker: params['marker']=marker
            url = INDEX+'?'+urllib.parse.urlencode(params)
            raw,m = self.get(url); evidence.append((raw,m,'archive_index'))
            root=ET.fromstring(raw)
            tags = 's:CommonPrefixes/s:Prefix' if delimiter else 's:Contents/s:Key'
            page=[x.text for x in root.findall(tags,NS)]; entries.extend(page)
            if root.findtext('s:IsTruncated',namespaces=NS)!='true': break
            next_marker=root.findtext('s:NextMarker',namespaces=NS) or (page[-1] if page else None)
            if not next_marker or next_marker==marker: raise ValueError('listing_pagination_stalled')
            marker=next_marker
        return entries,evidence


def normalise_row(row):
    if len(row)<8: raise ValueError('invalid_kline_shape')
    start,end=int(row[0]),int(row[6])
    # Official spot bulk format changes from milliseconds to microseconds in 2025.
    if start>=10**14: start//=1000
    if end>=10**14: end//=1000
    # A delisting/suspension can shorten the terminal daily candle. Retain its
    # actual close timestamp instead of dropping the losing market or inventing
    # trades through midnight. Observation remains at the next UTC boundary.
    if start%DAY or not start<=end<start+DAY: raise ValueError('not_utc_daily_bar')
    vals=[float(row[i]) for i in (1,2,3,4,5,7)]
    o,h,l,c,v,q=vals
    if not all(math.isfinite(x) for x in vals) or min(o,h,l,c)<=0 or min(v,q)<0:
        raise ValueError('invalid_ohlcv')
    if l>min(o,c) or h<max(o,c) or l>h: raise ValueError('inconsistent_ohlc')
    return (start,end,o,h,l,c,v,q)


def source(store, raw, meta, kind):
    sid=store.snapshot(Record(meta['url'],{'kind':kind},meta['fetched_ms'],raw))
    db=store.db
    old=db.execute('SELECT id,sha256 FROM sources WHERE url=?',(meta['url'],)).fetchone()
    if old and old['sha256']!=meta['sha256']: raise ValueError('source_revision_requires_new_dataset')
    db.execute('INSERT OR IGNORE INTO sources(url,snapshot_id,sha256,fetched_ms,kind) VALUES(?,?,?,?,?)',
               (meta['url'],sid,meta['sha256'],meta['fetched_ms'],kind))
    return db.execute('SELECT id FROM sources WHERE url=?',(meta['url'],)).fetchone()[0]


def ingest_zip(store, symbol, payload, start, end):
    raw,meta,check,cm=payload
    tokens=check.decode('utf-8').strip().split()
    if len(tokens)!=2 or tokens[0].lower()!=sha(raw) or tokens[1].lstrip('*')!=meta['url'].rsplit('/',1)[-1]:
        raise ValueError('publisher_checksum_mismatch:'+meta['url'])
    sid=source(store,raw,meta,'binance_monthly_zip')
    source(store,check,cm,'publisher_checksum')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=z.namelist()
        if len(names)!=1 or not names[0].endswith('.csv'): raise ValueError('unexpected_zip_members')
        if z.getinfo(names[0]).file_size>20_000_000: raise ValueError('csv_too_large')
        for row in csv.reader(io.StringIO(z.read(names[0]).decode('utf-8'))):
            try:values=normalise_row(row)
            except (ValueError,TypeError,OverflowError) as exc:
                # Preserve corrupt source observations without repairing numbers
                # or erasing the entire market/month. Replay sees a genuine gap.
                raw_row=json_text(row)
                store.db.execute('INSERT OR IGNORE INTO rejected_candles VALUES(?,?,?,?,?,?)',
                    ('binance',symbol,sid,sha(raw_row.encode()),raw_row,str(exc)))
                continue
            if not start<=values[0]<end: continue
            old=store.db.execute('SELECT open_ms,close_ms,o,h,l,c,volume,quote_volume FROM candles WHERE venue=? AND symbol=? AND open_ms=?',('binance',symbol,values[0])).fetchone()
            if old and tuple(old)!=values: raise ValueError('conflicting_candle:'+symbol)
            store.db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?)',('binance',symbol,*values,sid))


def collect(dbpath, cachepath, start, end, workers=12, symbols=None, memory_build=False):
    # Batch collection is single-writer; the OS releases this lock on process exit.
    import fcntl
    Path(dbpath).parent.mkdir(parents=True,exist_ok=True)
    with open(str(dbpath)+'.collect.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _collect(dbpath,cachepath,start,end,workers,symbols,memory_build)


def _collect(dbpath, cachepath, start, end, workers=12, symbols=None,memory_build=False):
    if start>=end or start%DAY or end%DAY or end>int(time.time()*1000):
        raise ValueError('invalid_collection_period')
    if memory_build and Path(dbpath).exists():raise ValueError('memory_build_requires_new_output_database')
    cache=Cache(cachepath); store=Store(':memory:' if memory_build else str(dbpath)); db=store.db
    # A batch archive does not need WAL. Avoid transporting/checkpointing live
    # WAL/SHM sidecars across hosted filesystem surfaces.
    db.execute('PRAGMA journal_mode=DELETE')
    db.executescript(SCHEMA)
    spec=dict(schema_version=1, venue='binance', quote='USDT', start_ms=start,end_exclusive_ms=end,
              universe='all_archived_USDT_pairs_not_current_exchangeInfo', symbols=symbols)
    old=db.execute("SELECT value FROM archive_meta WHERE key='collection_spec'").fetchone()
    if old and json.loads(old[0])!=spec: raise ValueError('dataset_spec_changed_use_new_database')
    db.execute('INSERT OR IGNORE INTO archive_meta VALUES(?,?)',('collection_spec',json_text(spec))); db.commit()
    db.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)',('collection_complete','false'));db.commit()
    prefixes,evidence=cache.listing('data/spot/monthly/klines/', '/')
    for raw,m,k in evidence: source(store,raw,m,k)
    universe=sorted(p.rstrip('/').split('/')[-1] for p in prefixes if p.rstrip('/').endswith('USDT'))
    if symbols:
        if set(symbols)-set(universe): raise ValueError('requested_symbol_not_archived')
        universe=sorted(symbols)
    write_json(Path(cachepath).parent/'universe.json',{'symbols':universe,'selection':spec})
    print(json.dumps({'phase':'discovered','pairs':len(universe)}),flush=True)
    tasks=[]; errors=[]
    def listing(s): return s,cache.listing('data/spot/monthly/klines/'+s+'/1d/')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in as_completed([pool.submit(listing,s) for s in universe]):
            s,(keys,ev)=f.result()
            for raw,m,k in ev: source(store,raw,m,k)
            for k in keys:
                match=re.search(r'-(\d{4}-\d{2})\.zip$',k)
                if match and start<=ms(match[1]+'-01')<end:
                    tasks.append((s,k))
    tasks.sort(key=lambda x:(x[0]!='BTCUSDT',x[0],x[1]))
    db.commit()
    write_json(Path(cachepath).parent/'download_plan.json',{'zip_count':len(tasks),'tasks':tasks})
    print(json.dumps({'phase':'planned','zip_count':len(tasks)}),flush=True)
    def download(task):
        s,k=task; raw,m=cache.get(BULK+k); check,cm=cache.get(BULK+k+'.CHECKSUM')
        return s,(raw,m,check,cm)
    done=0
    try:
        # Bounded waves avoid tens of thousands of pending response objects.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0,len(tasks),workers*4):
                futures={pool.submit(download,t):t for t in tasks[offset:offset+workers*4]}
                for f in as_completed(futures):
                    try:
                        s,payload=f.result(); ingest_zip(store,s,payload,start,end)
                    except Exception as exc:
                        errors.append({'task':futures[f],'error':str(exc)})
                        print(json.dumps({'phase':'error',**errors[-1]}),flush=True)
                        if cache.stop.is_set(): raise
                    done+=1
                db.commit()
                print(json.dumps({'phase':'download','done':done,'total':len(tasks),'errors':len(errors)}),flush=True)
        # Daily bucket timestamps do not identify the first/last actual trade.
        db.execute("DELETE FROM market_events WHERE evidence_level='observed_trade_bound_not_listing_notice'")
        for s,first,last,n in db.execute('SELECT symbol,min(open_ms),max(open_ms),count(*) FROM candles GROUP BY symbol').fetchall():
            for kind,when in [('first_observed_daily_bucket',first),('last_observed_daily_bucket',last)]:
                db.execute('INSERT OR IGNORE INTO market_events VALUES(?,?,?,?,?,?,?,?)',
                           ('binance',s,kind,when,None,int(time.time()*1000),BULK+'?prefix=data/spot/monthly/klines/'+s+'/1d/','observed_daily_bucket_not_precise_listing_notice'))
        check=[r[0] for r in db.execute('PRAGMA integrity_check')]
        if check!=['ok']:raise ValueError('sqlite_integrity_failure:'+str(check))
        db.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)',('collection_complete',json_text(not errors)))
        db.commit()
        result=coverage(db)
        result.update(errors=errors,planned_zip_count=len(tasks),processed_zip_count=done)
        if memory_build:
            temp=Path(str(dbpath)+'.tmp')
            target=sqlite3.connect(temp)
            try:
                db.backup(target)
                if [r[0] for r in target.execute('PRAGMA integrity_check')]!=['ok']:
                    raise ValueError('export_integrity_failure')
            finally:target.close()
            temp.replace(dbpath)
        write_json(Path(cachepath).parent/'collection.json',result)
        print(json.dumps(result),flush=True)
        if errors: raise RuntimeError('collection_incomplete_see_collection_json')
        return result
    finally:
        write_json(Path(cachepath).parent/'collection_errors.json',errors)
        store.close()


def coverage(db):
    markets=[]
    for s,n,first,last in db.execute('SELECT symbol,count(*),min(open_ms),max(open_ms) FROM candles GROUP BY symbol ORDER BY symbol'):
        gaps=(last-first)//DAY+1-n
        markets.append(dict(symbol=s,bars=n,first_ms=first,last_ms=last,missing_days=gaps))
    roots=[tuple(r) for r in db.execute('SELECT url,sha256 FROM sources ORDER BY url')]
    digest=hashlib.sha256()
    for row in db.execute('SELECT venue,symbol,open_ms,close_ms,o,h,l,c,volume,quote_volume FROM candles ORDER BY venue,symbol,open_ms'):
        digest.update((json_text(list(row))+'\n').encode())
    shortened=db.execute('SELECT count(*) FROM candles WHERE close_ms<open_ms+?-1',(DAY,)).fetchone()[0]
    rejected=db.execute('SELECT count(*) FROM rejected_candles').fetchone()[0]
    return dict(markets=len(markets),daily_bars=sum(x['bars'] for x in markets),
                source_count=len(roots),source_manifest_sha256=sha(json_text(roots).encode()),
                normalized_candles_sha256=digest.hexdigest(),shortened_daily_bars=shortened,rejected_candles=rejected,
                markets_with_gaps=sum(x['missing_days']>0 for x in markets),coverage=markets)


def verify_derived(path):
    """Regenerate normalized records from source ZIPs; detect derived-table tampering."""
    db=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
    try:
        if [r[0] for r in db.execute('PRAGMA integrity_check')]!=['ok']:
            raise ValueError('sqlite_integrity_failure')
        spec=json.loads(db.execute("SELECT value FROM archive_meta WHERE key='collection_spec'").fetchone()[0])
        start,end=spec['start_ms'],spec['end_exclusive_ms'];total=0
        for sid,url,raw in db.execute("SELECT s.id,s.url,r.raw FROM sources s JOIN snapshots r ON r.id=s.snapshot_id WHERE s.kind='binance_monthly_zip' ORDER BY s.url"):
            symbol=url.split('/klines/')[1].split('/')[0]
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                expected={};rejected=[]
                for row in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())):
                    try:v=normalise_row(row)
                    except (ValueError,TypeError,OverflowError) as exc:
                        rejected.append((json_text(row),str(exc)));continue
                    if start<=v[0]<end:
                        if v[0] in expected and expected[v[0]]!=v:raise ValueError('source_duplicate_conflict')
                        expected[v[0]]=v
            actual=[tuple(r) for r in db.execute('SELECT open_ms,close_ms,o,h,l,c,volume,quote_volume FROM candles WHERE source_id=? AND venue=? AND symbol=? ORDER BY open_ms',(sid,'binance',symbol))]
            if actual!=[expected[t] for t in sorted(expected)]:raise ValueError('derived_candle_mismatch:'+url)
            actual_rejected=db.execute('SELECT raw_row,reason FROM rejected_candles WHERE source_id=?',(sid,)).fetchall()
            if sorted(actual_rejected)!=sorted(set(rejected)):raise ValueError('rejected_candle_mismatch:'+url)
            total+=len(actual)
        if total!=db.execute('SELECT count(*) FROM candles').fetchone()[0]:raise ValueError('untraced_candles')
        return {'regenerated_candles':total}
    finally:db.close()


def load_histories(path, venue='binance'):
    db=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
    try:
        meta=dict(db.execute('SELECT key,value FROM archive_meta'))
        if not json.loads(meta.get('collection_complete','false')): raise ValueError('collection_incomplete')
        h={}
        for row in db.execute('SELECT symbol,open_ms,close_ms,o,h,l,c,volume,quote_volume FROM candles WHERE venue=? ORDER BY symbol,open_ms',(venue,)):
            if row[-1] is None:raise ValueError('quote_volume_unavailable_for_price_model')
            h.setdefault(row[0],[]).append(Bar(*row[1:]))
        return h,coverage(db)
    finally: db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True);p.add_argument('--cache',required=True)
    p.add_argument('--start',default='2018-01-01');p.add_argument('--end',default='2026-10-01',help='exclusive, complete months')
    p.add_argument('--workers',type=int,default=12);p.add_argument('--symbols',nargs='+')
    p.add_argument('--memory-build',action='store_true',help='build in RAM, then verify and atomically export a standalone SQLite file')
    a=p.parse_args()
    if not 1<=a.workers<=64: p.error('workers must be 1..64')
    if a.start[-2:]!='01' or a.end[-2:]!='01':p.error('monthly archive collection requires month boundaries')
    collect(a.db,a.cache,ms(a.start),ms(a.end),a.workers,a.symbols,a.memory_build)

if __name__=='__main__': main()
