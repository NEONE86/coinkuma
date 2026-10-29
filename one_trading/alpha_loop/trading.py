"""Post-decision visible-book virtual executions; never uses liquidation prices."""
import math
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from one_trading.autonomy.store import utc,instant
from one_trading.autonomy.market import fresh_book,number
from .observations import state_at

def fill(book,side,config,quantity,filters):
    age=(instant(utc())-instant(book['received_at'])).total_seconds()
    if not 0<=age<=config['max_input_age_seconds']:raise ValueError('STALE_BOOK')
    if book['spread_bps']>config['max_spread_bps']:raise ValueError('SPREAD_LIMIT')
    if book['bids'][0][0]>=book['asks'][0][0]:raise ValueError('CROSSED_BOOK')
    if side not in ('BUY','SELL'):raise ValueError('SHORT_UNSUPPORTED')
    lot=next(f for f in filters if f['filterType']=='LOT_SIZE')
    step=Decimal(lot['stepSize']);qty=Decimal(str(number(quantity,True)))
    if side=='BUY':qty=(qty/step).to_integral_value(rounding=ROUND_DOWN)*step
    elif qty%step!=0:raise ValueError('SELL_LOT_MISMATCH')
    if not Decimal(lot['minQty'])<=qty<=Decimal(lot['maxQty']):raise ValueError('LOT_SIZE')
    levels=book['asks'] if side=='BUY' else book['bids'];best=number(levels[0][0],True)
    remaining=float(qty);gross=0.;unadjusted=0.
    for price,size in levels:
        price=number(price,True);size=number(size,True)
        if abs(price/best-1)*10000>config['max_book_impact_bps']:break
        take=min(remaining,size*config['book_participation'])
        unadjusted+=take*price
        gross+=take*price*(1+(1 if side=='BUY' else -1)*config['slippage_bps_per_side']/10000)
        remaining-=take
        if remaining<1e-10:break
    if remaining>=1e-10 or float(qty)<=0:raise ValueError('INSUFFICIENT_DEPTH')
    minimum=max([float(f.get('minNotional',0)) for f in filters if f['filterType'] in ('NOTIONAL','MIN_NOTIONAL')]+[0])
    if unadjusted<minimum:raise ValueError('MIN_NOTIONAL')
    fee=gross*config['fee_bps_per_side']/10000
    mid_value=float(qty)*book['mid']
    market_cost=(unadjusted-mid_value) if side=='BUY' else (mid_value-unadjusted)
    slip=abs(gross-unadjusted)
    return dict(side=side,quantity=float(qty),gross_usdt=gross,fee_usdt=fee,
        spread_depth_cost_usdt=market_cost,slippage_cost_usdt=slip,
        cash_delta_usdt=-gross-fee if side=='BUY' else gross-fee,mid=book['mid'],
        average_price=gross/float(qty),quote_received_at=book['received_at'],
        quote_receipt_id=book['receipt_id'],simulated_at=utc(),funding_cost_usdt=0,
        funding_status='NOT_APPLICABLE_SPOT',execution='VISIBLE_BOOK_PAPER_NOT_EXCHANGE_FILL')

def books_now(store,universe):
    books={};errors={}
    for s in universe:
        try:books[s]=fresh_book(store,s)
        except Exception as exc:errors[s]=str(exc)
    return books,errors

def attempt_entry(store,config,case,snapshot,decision,track='NATIVE',eligible_at=None,observed_books=None):
    key=decision['id']+':'+track
    prior=store.get('alpha_entry_result',key,True)
    if prior:return prior
    if decision['status']!='DECIDED':
        return store.append('alpha_entry_result',key,dict(id=key,case_id=case['id'],arm=decision['arm'],track=track,
            status=decision['status'],fill=None,at=utc(),reason='NO_VALID_DECISION'))
    if decision['action']!='LONG':
        return store.append('alpha_entry_result',key,dict(id=key,case_id=case['id'],arm=decision['arm'],track=track,
            status='CASH',fill=None,at=utc(),reason='POLICY_NO_TRADE'))
    eligible=eligible_at or (instant(decision['ready_at'])+timedelta(seconds=config['execution_delay_seconds'])).isoformat()
    if instant(utc())<instant(eligible):return None
    expiry=min(instant(case['due_at']),instant(snapshot['cutoff'])+timedelta(seconds=config['decision_validity_seconds']))
    result=dict(id=key,case_id=case['id'],arm=decision['arm'],track=track,eligible_at=eligible,fill=None,symbol=decision['symbol'])
    if instant(utc())>expiry:
        result.update(status='EXPIRED',reason='DECISION_WINDOW_CLOSED',at=utc())
        return store.append('alpha_entry_result',key,result)
    books,errors=(observed_books,{}) if observed_books is not None else books_now(store,config['universe']);s=decision['symbol']
    try:
        if errors:raise ValueError('QUOTE_DATA_BLOCKED:'+str(errors))
        if any(instant(b['received_at'])<instant(eligible) for b in books.values()):raise ValueError('PRE_DECISION_QUOTE')
        state_time=utc();state=state_at(snapshot['fits'],books,state_time)[s];book=books[s]
        gap=state['reference_price']/book['asks'][0][0]-1
        cost=2*(config['fee_bps_per_side']+config['slippage_bps_per_side'])/10000+book['spread_bps']/10000
        if gap<=cost:raise ValueError('NO_EXECUTABLE_GAP')
        notional=config['capital_usdt']*min(config['max_exposure_fraction'],config['probe_risk_fraction']/config['stop_fraction'])
        quantity=notional/(book['asks'][0][0]*(1+(config['fee_bps_per_side']+config['slippage_bps_per_side'])/10000))
        execution=fill(book,'BUY',config,quantity,snapshot['filters'][s])
        if -execution['cash_delta_usdt']>config['capital_usdt']:raise ValueError('CASH_LIMIT')
        result.update(status='FILLED',fill=execution,reason='FROZEN_ENTRY_RULE',remaining_gap=gap,
                      estimated_roundtrip_cost=cost,state=state,
                      state_evaluated_at=state_time,
                      observation_receipts={s:b['receipt_id'] for s,b in books.items()})
    except Exception as exc:
        result.update(status='DATA_BLOCKED' if 'DATA_BLOCKED' in str(exc) else 'REJECTED_OPPORTUNITY',reason=str(exc))
    result['at']=utc()
    return store.append('alpha_entry_result',key,result)

def maintain_exits(store,config,case,snapshot):
    entries=[e for e in store.all('alpha_entry_result') if e['case_id']==case['id'] and e['status']=='FILLED']
    open_entries=[e for e in entries if not store.get('alpha_exit',e['id'],True)]
    if not open_entries:return
    books,errors=books_now(store,config['universe'])
    if errors:
        store.failure('alpha_exit_data',str(errors),case_id=case['id']);return
    state_time=utc();state=state_at(snapshot['fits'],books,state_time)
    for e in open_entries:
        s=e['symbol'];b=books[s];reason=None
        if instant(utc())>=instant(case['due_at']):reason='COMMON_HORIZON'
        elif b['bids'][0][0]<=e['fill']['average_price']*(1-config['stop_fraction']):reason='ADVERSE_CONTINUATION_STOP'
        elif state[s]['z']>=-config['normalization_abs_z']:reason='RESIDUAL_NORMALIZATION'
        if reason:
            try:
                x=fill(b,'SELL',config,e['fill']['quantity'],snapshot['filters'][s])
                store.append('alpha_exit',e['id'],dict(id=e['id'],case_id=case['id'],arm=e['arm'],track=e['track'],
                    symbol=s,fill=x,reason=reason,state=state[s],
                    state_evaluated_at=state_time,
                    observation_receipts={s:b['receipt_id'] for s,b in books.items()},
                    realized_pnl_usdt=e['fill']['cash_delta_usdt']+x['cash_delta_usdt']))
            except Exception as exc:store.failure('alpha_exit_fill',exc,entry_id=e['id'])
