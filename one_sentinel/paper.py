"""Decimal cash ledger; all execution is virtual and uses subsequent book receipts."""
from decimal import Decimal, ROUND_DOWN
from .observe import symbol_input
from .clocks import fresh

D=Decimal
def account(ledger, config):
    cash=D(config['initial_cash']); positions={}; realized=D(0)
    for f in ledger.all('fill'):
        s=f['symbol'];p=positions.setdefault(s,dict(qty=D(0),cost=D(0),opened=f['at'],added=False,reduced=False))
        q=D(f['qty']);gross=D(f['gross']);fee=D(f['fee'])
        if f['side']=='BUY':
            if not p['qty']: p.update(opened=f['at'],added=False,reduced=False)
            cash-=gross+fee;p['qty']+=q;p['cost']+=gross+fee
            if f['action']=='ADD':p['added']=True
        else:
            removed=p['cost']*q/p['qty'];cash+=gross-fee;realized+=gross-fee-removed;p['cost']-=removed;p['qty']-=q
            if f['action']=='REDUCE':p['reduced']=True
    pending=[d for d in ledger.all('decision') if d['action'] in ('ENTER','ADD','REDUCE','EXIT') and not ledger.get('execution',d['id'])]
    reserved=sum((D(d['reserve']) for d in pending),D(0))
    return dict(cash=cash,positions=positions,realized=realized,pending=pending,reserved=reserved)

def fill(ledger, policy, config, decision, receipt, now,current_timing=None):
    key=decision['id']
    if ledger.get('execution',key):return ledger.get('execution',key)
    if decision.get('timing'):
        if not current_timing or current_timing['session_id']!=decision['timing']['session_id']:
            return ledger.add('execution',key,dict(id=key,at=now,status='REJECTED',reason='NEW_MONOTONIC_EPOCH',book_receipt=receipt['id']))
        if current_timing['monotonic']<decision['ready_monotonic']:return None
        if current_timing['monotonic']>decision['expires_monotonic']:
            return ledger.add('execution',key,dict(id=key,at=now,status='REJECTED',reason='DECISION_EXPIRED',book_receipt=receipt['id']))
    if now<decision['ready_at']:return None
    b=receipt['parsed'];st=account(ledger,config);s=decision['symbol'];p=st['positions'].get(s,dict(qty=D(0),cost=D(0)))
    reason=None
    if now>decision['expires_at']:reason='DECISION_EXPIRED'
    elif receipt['received_at']<decision['ready_at'] or receipt['received_at']>now or now-receipt['received_at']>policy['book_fresh_seconds']:reason='BOOK_NOT_EXECUTABLE'
    elif b['symbol']!=s:reason='BOOK_SYMBOL'
    bid,ask=D(b['bid']),D(b['ask']);spread=(ask-bid)/((ask+bid)/2)*10000
    if spread>D(policy['max_spread_bps']):reason='SPREAD_COST_BLOCKED'
    buy=decision['action'] in ('ENTER','ADD');rate=D(config['fee_rate']);slip=D(config['slippage_bps'])/10000
    price=ask*(1+slip) if buy else bid*(1-slip)
    step=D(decision['lot_step'])
    qty=(D(decision['notional'])/price/step).to_integral_value(rounding=ROUND_DOWN)*step if buy else D(decision['quantity'])
    gross=qty*price;fee=gross*rate
    if qty<=0:reason='LOT_ZERO'
    if qty<D(decision['min_qty']) or gross<D(decision['min_notional']) or qty%step!=0:reason='INSTRUMENT_FILTER'
    if qty>D(b['ask_qty'] if buy else b['bid_qty'])*D(policy['depth_participation']):reason='LIQUIDITY_LIMIT'
    if buy:
        other=st['reserved']-D(decision['reserve'])
        total=sum((x['cost'] for x in st['positions'].values()),D(0))
        if gross+fee>st['cash']-other:reason='CASH_LIMIT'
        if gross+fee+p['cost']>D(config['max_symbol_cost']) or gross+fee+total+other>D(config['max_gross_cost']):reason='EXPOSURE_LIMIT'
        mark=Decimal(0)
        for asset,pos in st['positions'].items():
            if not pos['qty']:continue
            tick=symbol_input(ledger,'mini',asset)
            if not fresh(tick,'mini',asset,now,policy['price_fresh_seconds'],current_timing):reason='MARKET_EXPOSURE_UNKNOWN';continue
            mark+=Decimal(tick['parsed'][asset]['price'])*pos['qty']
        if mark+gross+other>D(config['max_gross_mark']) or p['qty']*ask+gross>D(config['max_symbol_mark']):reason='MARKET_EXPOSURE_LIMIT'
    elif qty>p['qty']:reason='INSUFFICIENT_INVENTORY'
    if not reason:
        row=dict(id=key,symbol=s,action=decision['action'],side='BUY' if buy else 'SELL',qty=str(qty),price=str(price),gross=str(gross),fee=str(fee),at=now,book_receipt=receipt['id'],decision_id=key,classification=receipt['classification'],actual_order=False)
        ledger.add('fill',key,row)
        if decision['action']=='ENTER':ledger.add('maturity',key,dict(id=key,symbol=s,due_at=now+policy['horizon_seconds'],state='PENDING',fill_id=key,score_eligible=False))
        if decision['action']=='EXIT':ledger.add('closure',key,dict(id=key,symbol=s,at=now,realized=str(account(ledger,config)['realized']),audit_complete=False))
    return ledger.add('execution',key,dict(id=key,at=now,status='REJECTED' if reason else 'PAPER_FILLED',reason=reason,book_receipt=receipt['id']))
