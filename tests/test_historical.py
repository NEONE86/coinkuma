"""Independent regressions for new archive and historical-universe risks."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from one_trading.historical_data import SCHEMA, normalise_row, ingest_zip, load_histories, Cache, ms, verify_derived
from one_trading.learning import D
from one_trading.observer import Bar, DAY, Store
from one_trading.portfolio import Book, Position
from one_trading.research import load_history
from one_trading.walkforward import HistoricalPanel, metrics, regime, writeoff, run

C=json.loads((Path(__file__).parent.parent/'config.json').read_text())

def bars(n=500,start=ms('2018-01-01'),price=100.):
    return [Bar(start+i*DAY,start+(i+1)*DAY-1,price,price*1.02,price*.98,price,10.,1000.) for i in range(n)]

def payload(rows):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr('X-1d-2018-01.csv','\n'.join(','.join(map(str,r)) for r in rows))
    raw=stream.getvalue();url='https://data.binance.vision/X-1d-2018-01.zip'
    m=dict(url=url,sha256=hashlib.sha256(raw).hexdigest(),fetched_ms=ms('2026-10-17'))
    check=(m['sha256']+'  X-1d-2018-01.zip\n').encode()
    cm=dict(url=url+'.CHECKSUM',sha256=hashlib.sha256(check).hexdigest(),fetched_ms=m['fetched_ms'])
    return raw,m,check,cm

class ArchiveTests(unittest.TestCase):
    def test_microseconds_are_normalized_not_confused_with_future(self):
        t=ms('2025-01-01');row=[t*1000,'1','2','.5','1','10',(t+DAY)*1000-1,'11']
        self.assertEqual(normalise_row(row),(t,t+DAY-1,1.,2.,.5,1.,10.,11.))

    def test_invalid_ohlc_rejected(self):
        with self.assertRaises(ValueError):normalise_row([0,1,.5,1,1,2,DAY-1,2])

    def test_delisting_day_retains_shortened_close_timestamp(self):
        close=3*60*60*1000-1
        self.assertEqual(normalise_row([0,1,2,.5,1,10,close,11])[1],close)

    def test_bar_cannot_extend_into_next_utc_day(self):
        with self.assertRaises(ValueError):normalise_row([0,1,2,.5,1,10,DAY,11])

    def test_derived_table_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.db';st=Store(str(p));st.db.executescript(SCHEMA)
            v=list(payload([[0,1,2,.5,1,10,DAY-1,10]]))
            v[1]['url']='https://data.binance.vision/data/spot/monthly/klines/XUSDT/1d/X-1d-2018-01.zip'
            ingest_zip(st,'XUSDT',v,0,DAY)
            st.db.execute('INSERT INTO archive_meta VALUES(?,?)',('collection_spec',json.dumps({'start_ms':0,'end_exclusive_ms':DAY})))
            st.db.commit()
            self.assertEqual(verify_derived(p)['regenerated_candles'],1)
            st.db.execute('UPDATE candles SET c=1.5');st.db.commit()
            with self.assertRaisesRegex(ValueError,'derived_candle_mismatch'):verify_derived(p)
            st.close()

    def test_checksum_and_conflict_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(str(Path(d)/'m.db'));st.db.executescript(SCHEMA)
            p=payload([[0,1,2,.5,1,10,DAY-1,10]])
            ingest_zip(st,'XUSDT',p,0,DAY)
            bad=list(p);bad[2]=b'0'*64+b'  X-1d-2018-01.zip\n'
            with self.assertRaises(ValueError):ingest_zip(st,'XUSDT',bad,0,DAY)
            st.close()

    def test_raw_zip_bytes_are_snapshotted(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(str(Path(d)/'m.db'));st.db.executescript(SCHEMA)
            p=payload([[0,1,2,.5,1,10,DAY-1,10]])
            ingest_zip(st,'XUSDT',p,0,DAY)
            self.assertEqual(st.db.execute("SELECT raw FROM snapshots WHERE path NOT LIKE '%.CHECKSUM'").fetchone()[0],p[0])
            st.close()

    def test_incomplete_collection_rejected_by_compatible_loader(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.db';st=Store(str(p));st.db.executescript(SCHEMA);st.close()
            with self.assertRaisesRegex(ValueError,'collection_incomplete'):load_history(p)

    def test_missing_database_does_not_create_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'absent.db'
            with self.assertRaises(sqlite3.OperationalError):load_history(p)
            self.assertFalse(p.exists())

    def test_tampered_cache_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            u='https://data.binance.vision/test';stem=Path(d)/hashlib.sha256(u.encode()).hexdigest()
            stem.with_suffix('.json').write_text(json.dumps({'url':u,'sha256':'bad'}))
            stem.with_suffix('.bin').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'cache_integrity'):Cache(d).get(u)

class ChronologyTests(unittest.TestCase):
    def panel(self):return HistoricalPanel({'BTCUSDT':bars(),'AAAUSDT':bars(),'BBBUSDT':bars()},dict(C))

    def test_future_volume_cannot_change_historical_universe(self):
        p=self.panel();day=ms('2018-10-01');before=p.features_on(day)
        h=copy.deepcopy(p.h);b=h['BBBUSDT'][-1]
        h['BBBUSDT'][-1]=Bar(b.open_ms,b.close_ms,b.open,b.high,b.low,b.close,1e20,1e25)
        self.assertEqual(before,HistoricalPanel(h,dict(C)).features_on(day))

    def test_gap_resets_calendar_history(self):
        p=self.panel();p.h['AAAUSDT'].pop(300);p=HistoricalPanel(p.h,dict(C))
        self.assertFalse(p.eligible('AAAUSDT',ms('2018-01-01')+320*DAY))

    def test_not_listed_before_first_trade(self):
        p=self.panel();p.h['AAAUSDT']=p.h['AAAUSDT'][300:];p=HistoricalPanel(p.h,dict(C))
        self.assertFalse(p.eligible('AAAUSDT',ms('2018-01-01')+290*DAY))

    def test_terminal_label_loss_is_not_dropped(self):
        p=self.panel();signal=ms('2018-01-01')+260*DAY;outcome=signal+29*DAY
        p.h['AAAUSDT']=p.h['AAAUSDT'][:270];p=HistoricalPanel(p.h,dict(C))
        p.features_on=lambda d:[{'symbol':'AAAUSDT','x':[1.]*D}]
        r=p.matured_group(outcome)
        self.assertEqual(len(r),1);self.assertEqual(r[0]['ys'],[-1.,-1.])

    def test_no_entry_means_no_fabricated_loss_label(self):
        p=self.panel();p.features_on=lambda d:[{'symbol':'MISSINGUSDT','x':[1.]*D}]
        self.assertEqual(p.matured_group(ms('2018-01-01')+300*DAY),[])

    def test_writeoff_is_loss_not_sale_or_cash_recovery(self):
        b=Book(900.);b.positions['X']=Position('X',1.,100.,0,80.,80.,100.)
        r=writeoff(b,'X',DAY)
        self.assertEqual(b.cash,900.);self.assertEqual(b.realized_pnl,-100.)
        self.assertEqual(b.fills,[]);self.assertEqual(r['proceeds'],0.)

    def test_regime_never_reads_current_day_close(self):
        b=bars();r=regime(b,300);x=b[300]
        b[300]=Bar(x.open_ms,x.close_ms,100,1e6,.01,1e6,10,1000)
        self.assertEqual(r,regime(b,300))

    def test_drawdown_includes_initial_capital(self):
        self.assertAlmostEqual(metrics([90.,95.,80.],100.)['max_drawdown'],-.2)

    def test_btc_costs_use_identical_test_dates(self):
        p=self.panel();start=ms('2019-01-01');end=start+30*DAY
        # Flat synthetic data exercises benchmark accounting, not profitability.
        r=run(p,dict(C),start,end)
        expected=(1-C['slippage_rate'])*(1-C['fee_rate'])/((1+C['slippage_rate'])*(1+C['fee_rate']))-1
        self.assertAlmostEqual(r['summary']['btc']['total_return'],expected)
        self.assertEqual(len(r['equity']),30)

if __name__=='__main__':unittest.main()
