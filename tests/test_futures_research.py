"""Synthetic chronology/cost fixtures do not establish futures profitability."""
from dataclasses import replace
import io
import unittest
import zipfile
from one_trading.futures_4h_research import Observation,Structure,DataNotFit,asof,direction,funding_cashflow,data_gate,HOUR
from one_trading.futures_data_audit import inspect_zip,parse_row
from one_trading.historical_data import ms
from scripts.validate_futures_4h import reconstruct


def zipped(text):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('fixture.csv',text)
    return b.getvalue()


class FuturesSourceTests(unittest.TestCase):
    def inspect(self,text,stream='metrics'):
        return inspect_zip(zipped(text),stream,'BTCUSDT','BTCUSDT-metrics-2022-01-01.zip' if stream=='metrics' else 'BTCUSDT-1h-2022-01.zip',ms('2022-01-01'),ms('2022-02-01'))
    def test_identical_duplicates_retained_as_evidence_not_two_OI_points(self):
        r=self.inspect('create_time,symbol,sum_open_interest,sum_open_interest_value\n2022-01-01 00:00:00,BTCUSDT,10,100\n2022-01-01 00:00:00,BTCUSDT,10,100\n')
        self.assertEqual(r['counts']['duplicate_rows'],1);self.assertEqual(r['counts']['usable_unique_rows'],1)
    def test_conflicting_timestamp_quarantines_all_versions(self):
        r=self.inspect('create_time,symbol,sum_open_interest,sum_open_interest_value\n2022-01-01 00:00:00,BTCUSDT,10,100\n2022-01-01 00:00:00,BTCUSDT,11,110\n')
        self.assertEqual(r['counts']['conflicting_timestamps'],1);self.assertEqual(r['counts']['usable_unique_rows'],0)
    def test_out_of_order_is_counted_and_input_is_not_silently_trusted(self):
        r=self.inspect('2022-01-01 00:05:00,BTCUSDT,10,100\n2022-01-01 00:00:00,BTCUSDT,10,100\n')
        self.assertEqual(r['counts']['out_of_order_transitions'],1)
    def test_missing_nan_and_wrong_symbol_not_zero_repaired(self):
        for value in ('nan','inf',''):
            r=self.inspect(f'2022-01-01 00:00:00,BTCUSDT,{value},100\n')
            self.assertEqual(r['counts']['invalid_rows'],1)
        r=self.inspect('2022-01-01 00:00:00,ETHUSDT,10,100\n')
        self.assertEqual(r['counts']['invalid_rows'],1)
    def test_headerless_kline_first_row_not_lost(self):
        t=ms('2022-01-01');row=f'{t},10,11,9,10,20,{t+HOUR-1},200,30,10,100,0\n'
        a=self.inspect(row,'klines');b=self.inspect('open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n'+row,'klines')
        self.assertEqual(a['counts']['usable_unique_rows'],1);self.assertEqual(a['normalized_sha256'],b['normalized_sha256'])
    def test_actual_funding_milliseconds_and_variable_interval_preserved(self):
        t=ms('2022-01-01')+6
        self.assertEqual(parse_row('fundingRate','BTCUSDT',[str(t),'4','-.0001']),(t,4.,-.0001))
    def test_cross_day_rows_are_quarantined(self):
        r=self.inspect('2022-01-02 00:00:00,BTCUSDT,10,100\n')
        self.assertEqual(r['counts']['invalid_rows'],1)
    def test_independent_parser_reproduces_conflicts_gaps_and_headerless_rows(self):
        text='create_time,symbol,sum_open_interest,sum_open_interest_value\n2022-01-01 00:05:00,BTCUSDT,10,100\n2022-01-01 00:00:00,BTCUSDT,10,100\n2022-01-01 00:00:00,BTCUSDT,11,110\n'
        a=self.inspect(text);counts,sha,times,columns=reconstruct(zipped(text),'metrics','BTCUSDT','BTCUSDT-metrics-2022-01-01.zip',ms('2022-01-01'),ms('2022-02-01'))
        from collections import Counter
        self.assertEqual(counts,Counter(a['counts']));self.assertEqual(sha,a['normalized_sha256'])
        self.assertEqual(len(times),1);self.assertEqual(columns,[])
    def test_source_tamper_changes_independent_normalized_digest(self):
        args=('metrics','BTCUSDT','BTCUSDT-metrics-2022-01-01.zip',ms('2022-01-01'),ms('2022-02-01'))
        a=reconstruct(zipped('2022-01-01 00:00:00,BTCUSDT,10,100\n'),*args)
        b=reconstruct(zipped('2022-01-01 00:00:00,BTCUSDT,11,110\n'),*args)
        self.assertNotEqual(a[1],b[1])


class FuturesChronologyTests(unittest.TestCase):
    def fixture(self,sign=-1):
        t=ms('2024-01-03')
        oi=[Observation(t-24*HOUR,t-24*HOUR+1,100,True),Observation(t,t+1,106,True)]
        fs=[Observation(t-i*8*HOUR,t-i*8*HOUR+10,sign*.0001,True) for i in (1,2,3)]
        st=Structure(t,t-1,t+1,True,tuple(t-i*HOUR for i in (4,3,2,1)),100,101,100,60,100,-.001)
        return t,oi,fs,st
    def test_long_and_short_exact_rule_boundaries(self):
        t,o,f,s=self.fixture();self.assertEqual(direction(t,o,f,s),'LONG')
        t,o,f,s=self.fixture(1);s=replace(s,price_close=99,taker_quote=40,basis=.001)
        self.assertEqual(direction(t,o,f,s),'SHORT')
    def test_event_time_is_not_publication_time(self):
        t,o,f,s=self.fixture();o=[replace(v,available_ms=None,availability_evidenced=False) for v in o]
        with self.assertRaises(DataNotFit):direction(t,o,f,s)
    def test_later_available_and_future_events_cannot_change_prior_asof(self):
        t,o,_,_=self.fixture();old=asof(o,t,t+600_000,900_000)
        extra=[Observation(t,t+HOUR,999,True),Observation(t+1,t+2,888,True)]
        self.assertEqual(old,asof(o+extra,t,t+600_000,900_000))
    def test_last_modified_without_availability_proof_is_rejected(self):
        t=ms('2024-01-01')
        with self.assertRaises(DataNotFit):asof([Observation(t,t+1,100,False)],t,t+600_000,900_000)
    def test_missing_hour_and_future_structure_rejected(self):
        t,o,f,s=self.fixture()
        for bad in (replace(s,hourly_opens=s.hourly_opens[:-1]),replace(s,last_input_close_ms=t+1),replace(s,available_ms=t+HOUR)):
            with self.assertRaises(DataNotFit):direction(t,o,f,bad)
    def test_unsettled_or_duplicate_funding_not_a_predictive_input(self):
        t,o,f,s=self.fixture()
        with self.assertRaises(DataNotFit):direction(t,o,[replace(v,event_ms=t+1) for v in f],s)
    def test_actual_funding_debits_longs_credits_shorts(self):
        self.assertAlmostEqual(funding_cashflow(2,100,.001),-.2)
        self.assertAlmostEqual(funding_cashflow(-2,100,.001),.2)
        self.assertAlmostEqual(funding_cashflow(2,100,-.001),.2)
    def test_no_metrics_manufactured_on_data_failure(self):
        r=data_gate(dict(historical_availability_verified_rows=0,publisher_checksums_verified=1,source_zip_count=1))
        self.assertEqual(r['decision'],'NEEDS_MORE_EVIDENCE');self.assertFalse(r['strategy_backtest_executed'])
        self.assertTrue(all(v is None for v in r['performance_metrics'].values()))
    def test_partial_availability_and_empty_archive_never_pass(self):
        for rows,count in ((1,1),(0,0)):
            r=data_gate(dict(historical_availability_verified_rows=rows,required_signal_observations=100,verified_signal_observations=1,
                historical_contract_terms_verified=True,exact_funding_settlement_marks_verified=True,publisher_checksums_verified=count,source_zip_count=count))
            self.assertFalse(r['data_gate_passed'])


if __name__=='__main__':unittest.main()
