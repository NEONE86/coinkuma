"""Only facts supported by the actual input are extracted. No WS client enabled."""
from decimal import Decimal
from .store import digest
from .clocks import assess

PARSER_VERSION='ITEM_ISOLATION_V2'

def parse_revision(kind,data,received,classification,timing,sample,clock_policy):
    """Preserve the envelope; reject a bad symbol without rejecting its peers."""
    if kind not in ('mini','bar'):return parse(kind,data,received),[],{}
    if kind=='mini' and (not isinstance(data,list) or not data):raise ValueError('INVALID_MINI_ENVELOPE')
    values=data if kind=='mini' else [data]
    duplicates={}
    for value in values:
        if isinstance(value,dict) and isinstance(value.get('s'),str):duplicates[value['s']]=duplicates.get(value['s'],0)+1
    parsed={};items=[];times={}
    for i,value in enumerate(values):
        symbol=value.get('s') if isinstance(value,dict) else None
        item=dict(index=i,symbol=symbol,state='INVALID',parser_version=PARSER_VERSION)
        try:
            if not isinstance(symbol,str) or not symbol.isalnum():raise ValueError('INVALID_SYMBOL')
            if duplicates[symbol]>1:raise ValueError('DUPLICATE_SYMBOL')
            event=value['E']
            if type(event) is not int:raise ValueError('INVALID_SOURCE_TIME')
            event=event/1000
            if kind=='bar':
                if type(value['k']['T']) is not int:raise ValueError('INVALID_SOURCE_TIME')
                event=max(event,value['k']['T']/1000)
            # Parse structure/numbers independently from source-clock eligibility.
            normalized=parse(kind,[value] if kind=='mini' else value,float('inf'))
            if classification=='SYNTHETIC' and timing is None:
                t=dict(state='VALID' if event<=received else 'FUTURE',source_time=event,
                       age_upper=max(0,received-event),version='SYNTHETIC_EXACT_V1')
            else:t=assess(event,timing,sample,clock_policy)
            if kind=='bar':
                t['freshness_source_time']=value['k']['T']/1000
                if 'age_upper' in t:t['age_upper']+=event-t['freshness_source_time']
            item.update(t)
            if t['state']=='VALID':
                if kind=='mini':parsed.update(normalized);times[symbol]=t
                else:parsed=normalized;times['@bar']=t
        except (ValueError,KeyError,TypeError,IndexError,ArithmeticError) as exc:
            item.update(state='INVALID',reason=str(exc))
        items.append(item)
    return parsed,items,times

def positive(value, zero=False):
    v=Decimal(str(value))
    if not v.is_finite() or (v < 0 if zero else v <= 0): raise ValueError('INVALID_NUMBER')
    return str(v)

def parse(kind, data, received):
    if kind == 'metadata':
        out={}
        if not isinstance(data.get('symbols'), list) or not data['symbols']: raise ValueError('EMPTY_METADATA')
        for r in data['symbols']:
            if r.get('quoteAsset') != 'USDT': continue
            if r['symbol'] in out: raise ValueError('DUPLICATE_SYMBOL')
            if type(r.get('isSpotTradingAllowed')) is not bool: raise ValueError('SPOT_BOOLEAN')
            filters={f['filterType']:f for f in r.get('filters',[])};lot=filters.get('LOT_SIZE',{});minimum=filters.get('NOTIONAL',filters.get('MIN_NOTIONAL',{}))
            out[r['symbol']]={'status':r['status'],'spot':r['isSpotTradingAllowed'],'step':positive(lot['stepSize']) if lot.get('stepSize') else None,'min_qty':positive(lot.get('minQty','0'),True),'min_notional':positive(minimum.get('minNotional','0'),True)}
        return out
    if kind == 'mini':
        out={}
        for r in data:
            if r.get('e') != '24hrMiniTicker' or r['E']/1000 > received: raise ValueError('FUTURE_OR_WRONG_EVENT')
            if r['s'] in out: raise ValueError('DUPLICATE_SYMBOL')
            out[r['s']]={'price':positive(r['c']),'rolling_24h_volume':positive(r['v'], True),'rolling_24h_quote_volume':positive(r['q'], True),'short_quote_volume':None,'source_time':r['E']/1000}
        return out
    if kind == 'bar':
        k=data['k']
        if k['i'] != '1m' or type(k['x']) is not bool or not k['x'] or k['T']/1000 > received or data['E']/1000 > received: raise ValueError('UNCLOSED_OR_FUTURE_BAR')
        if k['T']-k['t'] != 59999: raise ValueError('NOT_ONE_MINUTE')
        return {'symbol':data['s'],'open':k['t']/1000,'close':k['T']/1000,'quote_volume':positive(k['q'],True)}
    if kind == 'book':
        bid=positive(data['bids'][0][0]); ask=positive(data['asks'][0][0])
        if Decimal(bid) > Decimal(ask): raise ValueError('CROSSED_BOOK')
        return dict(symbol=data['symbol'],bid=bid,ask=ask,bid_qty=positive(data['bids'][0][1]),ask_qty=positive(data['asks'][0][1]))
    raise ValueError('UNSUPPORTED_SOURCE')

def latest(ledger, source):
    return ledger.latest_input(source)

def symbol_input(ledger, source, symbol):
    return ledger.latest_input(source,symbol)

def membership(ledger, policy, now, open_symbols=()):
    metadata=latest(ledger,'metadata')
    allowed={s for s,r in (metadata['parsed'] if metadata else {}).items() if r['status']=='TRADING' and r['spot']}
    events=ledger.all('event')
    ranked={s:(0,0) for s in policy['deep_initial'] if s in allowed}
    for event in events:
        if event['symbol'] in allowed and now-event['at'] <= policy['deep_release_seconds']:
            ranked[event['symbol']]=max(ranked.get(event['symbol'],(0,0)),(3,event['at']))
    for r in ledger.all('price_move'):
        s=r['symbol']
        if s in allowed and 0<=now-r['at']<=policy['price_fresh_seconds']:ranked[s]=max(ranked.get(s,(0,0)),(1,r['at']))
    previous=ledger.all('membership')
    if previous:
        last=previous[-1]
        for s in last['symbols']:
            if s in allowed and now-last['at']<policy['deep_min_residency_seconds']: ranked[s]=max(ranked.get(s,(0,0)),(2,last['at']))
    for s in open_symbols:ranked[s]=(4,now) # OPEN evidence remains monitored even if delisted.
    chosen=sorted(ranked,key=lambda s:(-ranked[s][0],-ranked[s][1],s))[:policy['deep_slots']]
    if not previous or previous[-1]['symbols']!=chosen:
        ledger.add('membership',digest([now,chosen]),dict(at=now,symbols=chosen,reason='OPEN > STRUCTURAL > RESIDENCY > PRICE > INITIAL; lexical tie break'))
    return chosen
