"""Independent Bitstamp OHLCV reference and curated official market events.

Not an exchange-complete/delist-complete trading universe. USD is not USDT.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics
import time
import urllib.parse
from .historical_data import Cache, SCHEMA, ms, source, write_json, normalise_row
from .observer import DAY, Store

PAIRS=['btcusd','ethusd','ltcusd','xrpusd','bchusd']
NOTICES=[
 {'url':'https://www.binance.com/en/support/announcement/detail/f73b083ba6834771b07dbe5319917ae5',
  'symbols':['ANTUSDT','MULTIUSDT','XMRUSDT'],'kind':'official_delisting',
  'effective':'2024-02-20T03:00:00','published':'2024-02-06T07:00:00',
  'known_from_conservative':'2024-02-08T00:00:00','last_update':'2024-02-07',
  'verified_fact':'Spot trading ceases; the separate futures and margin dates are not spot dates.'},
 {'url':'https://www.binance.com/en/support/announcement/detail/836af23f13f24be08d99ec157ed1edac',
  'symbols':['SCRTUSDT'],'kind':'official_pair_listing',
  'effective':'2022-01-21T12:00:00','published':'2022-01-20T10:57:00',
  'known_from_conservative':'2022-01-21T00:00:00','last_update':None,
  'verified_fact':'SCRT/USDT opens; this is a pair listing, not token creation or first listing on any exchange.'}
]


def collect(out,start,end):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    cache=Cache(out/'cache');st=Store(str(out/'reference.sqlite3'));st.db.executescript(SCHEMA)
    for pair in PAIRS:
        for a in range(start,end,1000*DAY):
            z=min(end,a+1000*DAY)
            params=dict(step=86400,limit=(z-a)//DAY,end=z//1000-1,exclude_current_candle='true')
            url='https://www.bitstamp.net/api/v2/ohlc/'+pair+'/?'+urllib.parse.urlencode(params)
            raw,m=cache.get(url);rows=json.loads(raw)['data']['ohlc']
            sid=source(st,raw,m,'bitstamp_native_ohlcv_json')
            for r in rows:
                t=int(r['timestamp'])*1000
                if not a<=t<z:continue
                # Validate the OHLC/base volume with the common validator; quote
                # volume is absent in Bitstamp's schema and MUST stay NULL.
                values=normalise_row([t,r['open'],r['high'],r['low'],r['close'],r['volume'],t+DAY-1,0])
                record=('bitstamp',pair.upper(),*values[:-1],None,sid)
                old=st.db.execute('SELECT * FROM candles WHERE venue=? AND symbol=? AND open_ms=?',record[:3]).fetchone()
                if old and tuple(old)[:-1]!=record[:-1]:raise ValueError('reference_conflict')
                st.db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?)',record)
            st.db.commit()
        print(pair,st.db.execute('SELECT count(*) FROM candles WHERE symbol=?',(pair.upper(),)).fetchone()[0],flush=True)
    notices=[]
    for notice in NOTICES:
        n=dict(notice)
        try:
            raw,m=cache.get(n['url'])
            if len(raw)<100 or b'<html' not in raw.lower():raise ValueError('not_a_complete_html_response')
            sid=source(st,raw,m,'official_notice_html')
            n.update(raw_snapshot_saved=True,sha256=m['sha256'])
        except Exception as exc:
            # Structured facts were independently reviewed on the cited public
            # page; never describe a blocked HTML fetch as a raw HTTP capture.
            n.update(raw_snapshot_saved=False,fetch_error=str(exc))
        n['fact_reviewed_at_utc']='2026-10-17';n['used_in_strategy']=False
        for s in n['symbols']:
            st.db.execute('INSERT OR IGNORE INTO market_events VALUES(?,?,?,?,?,?,?,?)',
                ('binance',s,n['kind'],ms(n['effective']),ms(n['known_from_conservative']),int(time.time()*1000),n['url'],'curated_official_notice_not_complete_event_history'))
        notices.append(n)
        if cache.stop.is_set():break
    report={'scope':'5 fixed major USD spot reference pairs; no claim of complete Bitstamp historical universe',
            'quote_volume':'unavailable, stored NULL, not used as Ridge universe input',
            'coverage':[dict(symbol=s,bars=n,first_ms=f,last_ms=l) for s,n,f,l in st.db.execute('SELECT symbol,count(*),min(open_ms),max(open_ms) FROM candles GROUP BY symbol')],
            'official_notices':notices}
    st.db.commit();st.close();write_json(out/'reference_collection.json',report)
    return report


def compare(binance,reference,output):
    a=sqlite3.connect(binance);b=sqlite3.connect(reference);results={}
    for s in PAIRS:
        symbol=s[:-3].upper()+'USDT'
        x=dict(a.execute('SELECT open_ms,c FROM candles WHERE venue=? AND symbol=?',('binance',symbol)))
        y=dict(b.execute('SELECT open_ms,c FROM candles WHERE venue=? AND symbol=?',('bitstamp',s.upper())))
        common=sorted(x.keys()&y.keys());diff=sorted(abs(x[t]/y[t]-1) for t in common)
        if diff:results[s]={'matched_days':len(common),'median_absolute_close_basis':statistics.median(diff),
                           'p99_absolute_close_basis':diff[min(len(diff)-1,int(len(diff)*.99))],
                           'max_absolute_close_basis':max(diff),'over_5_percent_days':sum(d>.05 for d in diff)}
    a.close();b.close();write_json(output,{'warning':'Different venues and USD vs USDT; discrepancies are not automatically bad data','pairs':results})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--start',default='2018-01-01');p.add_argument('--end',default='2026-10-01')
    a=p.parse_args();collect(a.output,ms(a.start),ms(a.end))
