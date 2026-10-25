"""Fail-closed research primitives. Not a futures execution/account adapter."""
from dataclasses import dataclass
import math
import statistics

HOUR=3_600_000


@dataclass(frozen=True)
class Observation:
    event_ms:int
    available_ms:int|None
    value:float
    availability_evidenced:bool=False


class DataNotFit(ValueError):pass


@dataclass(frozen=True)
class Structure:
    boundary_ms:int
    last_input_close_ms:int
    available_ms:int|None
    availability_evidenced:bool
    hourly_opens:tuple
    price_open:float
    price_close:float
    previous_close:float
    taker_quote:float
    total_quote:float
    basis:float


def asof(observations,event_until,observed_ms,max_age_ms):
    valid=[v for v in observations if v.availability_evidenced and v.available_ms is not None
           and v.event_ms<=event_until and v.event_ms<=v.available_ms<=observed_ms
           and math.isfinite(v.value)]
    if not valid:raise DataNotFit('historical_availability_not_evidenced')
    latest=max(valid,key=lambda v:(v.event_ms,v.available_ms))
    if event_until-latest.event_ms>max_age_ms:raise DataNotFit('stale_input')
    return latest


def direction(boundary,oi,funding,structure):
    """Evaluate only explicit as-of OI/funding; structure inputs must be completed."""
    observed=boundary+600_000
    s=structure
    if (s.boundary_ms!=boundary or s.last_input_close_ms>=boundary or not s.availability_evidenced
        or s.available_ms is None or not s.last_input_close_ms<=s.available_ms<=observed
        or s.hourly_opens!=tuple(boundary-i*HOUR for i in (4,3,2,1))):
        raise DataNotFit('incomplete_or_unavailable_structure')
    current=asof(oi,boundary,observed,900_000)
    previous=asof(oi,boundary-24*HOUR,observed,900_000)
    if min(current.value,previous.value)<=0:raise DataNotFit('nonpositive_OI')
    known=sorted([v for v in funding if v.availability_evidenced and v.available_ms is not None
                  and v.event_ms<boundary and v.event_ms<=v.available_ms<=observed and math.isfinite(v.value)],key=lambda v:v.event_ms)
    if len(known)<3 or boundary-known[-1].event_ms>24*HOUR:raise DataNotFit('settled_funding_unavailable')
    if len({v.event_ms for v in known[-3:]})!=3:raise DataNotFit('ambiguous_funding')
    values=[s.price_open,s.price_close,s.previous_close,s.taker_quote,s.total_quote,s.basis]
    if not all(math.isfinite(v) for v in values) or min(s.price_open,s.price_close,s.previous_close)<=0 or s.total_quote<=0 or not 0<=s.taker_quote<=s.total_quote:raise DataNotFit('structure_values')
    rate=statistics.fmean(v.value for v in known[-3:]);growth=current.value/previous.value-1;share=s.taker_quote/s.total_quote
    if growth<.05:return 'WAIT'
    if rate<=-.0001 and s.price_close>s.price_open and s.price_close>s.previous_close and share>=.55 and s.basis<=0:return 'LONG'
    if rate>=.0001 and s.price_close<s.price_open and s.price_close<s.previous_close and share<=.45 and s.basis>=0:return 'SHORT'
    return 'WAIT'


def funding_cashflow(signed_quantity,settlement_mark,realized_rate):
    if not all(math.isfinite(v) for v in (signed_quantity,settlement_mark,realized_rate)) or settlement_mark<=0 or abs(realized_rate)>=1:
        raise DataNotFit('invalid_actual_settlement')
    return -signed_quantity*settlement_mark*realized_rate


def data_gate(report):
    reasons=[]
    if (report['historical_availability_verified_rows']==0 or report.get('required_signal_observations',0)<=0
        or report.get('verified_signal_observations',0)!=report.get('required_signal_observations')):
        reasons.append('OI_HISTORICAL_AVAILABILITY_UNVERIFIED')
    # These are explicit evidence requirements, not assumptions inferred from
    # valid price checksums or today's contract metadata.
    if not report.get('historical_contract_terms_verified',False):reasons.append('HISTORICAL_FUTURES_TERMS_UNVERIFIED')
    if not report.get('exact_funding_settlement_marks_verified',False):reasons.append('FUNDING_SETTLEMENT_MARKS_UNVERIFIED')
    if report['source_zip_count']<=0 or report['publisher_checksums_verified']!=report['source_zip_count']:reasons.append('SOURCE_CHECKSUM_INCOMPLETE')
    return dict(decision='NEEDS_MORE_EVIDENCE',data_gate_passed=not reasons,
        reasons=reasons or ['PERFORMANCE_AND_FUTURES_ENGINE_EVIDENCE_NOT_EXECUTED'],
        strategy_backtest_executed=False,performance_metrics={k:None for k in (
            'total_return','cagr','max_drawdown','profit_factor','win_rate','trade_count','average_holding_days',
            'average_exposure','monthly_returns','worst_losing_streak','monthly_krw_pnl','target_hit_frequency')},
        simulated_or_live_orders=0,leverage_applied=False,
        implementation='Signal primitives only; no futures account engine or performance simulation approved by this data gate')
