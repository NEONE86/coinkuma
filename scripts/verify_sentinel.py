"""Independent standard-library verifier. Imports NO project implementation.

Recomputes raw parsing, chronological bindings, eligibility of ENTER, delayed
book price/lot/fee/cash/inventory/PnL, chain integrity and event/outbox linkage.
Not an investment-performance validator or proof of an external timestamp.
"""
import argparse,hashlib,json
from decimal import Decimal as D, ROUND_DOWN
from pathlib import Path

def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(x):return hashlib.sha256(x).hexdigest()
def fresh_at(row,key,decision,ttl,source_time):
    t=row.get('time_validity',{}).get(key)
    if not t or t.get('version')=='SYNTHETIC_EXACT_V1':return 0<=decision['at']-source_time<=ttl
    now=decision.get('timing');then=row.get('validity_timing',row.get('timing'))
    if not now or not then or now['session_id']!=then['session_id']:return False
    elapsed=now['monotonic']-then['monotonic']
    return t['state']=='VALID' and elapsed>=0 and t['age_upper']+elapsed<=ttl
def verify(root):
    root=Path(root);records=[json.loads(x) for x in (root/'ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    index={};prev='0'*64;fills=[];cash=None;positions={};realized=D(0);fees=D(0);latest_bars={};bar_history={}
    for n,r in enumerate(records,1):
        assert r['seq']==n and r['prev']==prev,'CHAIN_SEQUENCE'
        b=json.loads(r['body']);assert sha(canonical([prev,r['kind'],r['id'],b]).encode())==r['hash'],'CHAIN_HASH';prev=r['hash']
        assert (r['kind'],r['id']) not in index,'DUPLICATE_ID';index[r['kind'],r['id']]=b
        k=r['kind']
        if k=='contract':
            policy=b['policy'];cfg=b['account'];cash=D(cfg['initial_cash'])
            assert sha(canonical(policy).encode())==b['policy_hash'] and sha(canonical(cfg).encode())==b['account_hash'],'CONTRACT_HASH'
        if k=='receipt':
            raw=(root/'raw'/f"{b['sha256']}.bin").read_bytes();assert sha(raw)==b['sha256'],'RAW_HASH'
            assert b['received_at']<=b['processed_at'],'FUTURE_RECEIPT'
        if k=='input':
            receipt=index['receipt',b.get('receipt_id',r['id'])]
            immutable=('source','sha256','bytes','received_at','processed_at','classification','origin','context','timing','provenance')
            assert b['id']==r['id'] and all(b.get(field)==receipt.get(field) for field in immutable),'INPUT_RECEIPT_BINDING'
            assert b.get('release_id') or r['id']==receipt['id'],'INPUT_RECEIPT_ID_BINDING'
            original=json.loads((root/'raw'/f"{receipt['sha256']}.bin").read_bytes());parsed=b['parsed'];source=receipt['source']
            if isinstance(original,dict) and 'stream' in original:original=original['data']
            if source=='mini':
                for item_index,item in enumerate(original):
                    symbol=item.get('s') if isinstance(item,dict) else None
                    if not isinstance(symbol,str) or symbol not in parsed:
                        release=index.get(('interpretation_release',b.get('release_id')))
                        rev=index.get(('interpretation',b.get('receipt_id',b['id'])))
                        assert rev and (release and release['item_index']!=item_index or any(x['index']==item_index and x['symbol']==symbol and x['state']!='VALID' for x in rev['items'])),'UNEXPLAINED_ITEM_OMISSION'
                        continue
                    p=parsed[item['s']];assert D(p['price'])==D(item['c']) and D(p['rolling_24h_quote_volume'])==D(item['q']) and p['short_quote_volume'] is None,'RAW_MINI_BINDING'
                    assert p['source_time']==item['E']/1000,'RAW_TIME'
                    if b.get('time_validity'):assert b['time_validity'][item['s']]['state']=='VALID','CLOCK_STATE'
                    else:assert p['source_time']<=b['received_at'],'RAW_TIME'
            elif source=='metadata':
                expected={}
                for s in original['symbols']:
                    if s.get('quoteAsset')!='USDT':continue
                    fs={f['filterType']:f for f in s.get('filters',[])};lot=fs.get('LOT_SIZE',{});minimum=fs.get('NOTIONAL',fs.get('MIN_NOTIONAL',{}))
                    expected[s['symbol']]=dict(status=s['status'],spot=s['isSpotTradingAllowed'],step=str(D(lot['stepSize'])) if lot.get('stepSize') else None,min_qty=str(D(lot.get('minQty','0'))),min_notional=str(D(minimum.get('minNotional','0'))))
                assert parsed==expected,'RAW_METADATA_BINDING'
            elif source=='bar':
                assert parsed['symbol']==original['s'] and D(parsed['quote_volume'])==D(original['k']['q']) and parsed['open']==original['k']['t']/1000 and parsed['close']==original['k']['T']/1000,'RAW_BAR_BINDING'
                symbol=parsed['symbol'];old=latest_bars.get(symbol)
                assert not old or (parsed['open'],parsed['close'])>(old['parsed']['open'],old['parsed']['close']),'BAR_LOGICAL_TIME_REGRESSION'
                latest_bars[symbol]=b;bar_history[symbol,parsed['open']]=b
                if b.get('time_validity'):assert b['time_validity']['@bar']['state']=='VALID','CLOCK_STATE'
                else:assert parsed['close']<=b['received_at'],'RAW_BAR_BINDING'
            elif source=='book':assert all(D(parsed[p])==D(original[side][0][i]) for p,side,i in [('bid','bids',0),('ask','asks',0),('bid_qty','bids',1),('ask_qty','asks',1)]),'RAW_BOOK_BINDING'
        reason=b.get('reason') if k=='clock_pending_result' else b.get('error') if k=='quarantine' else None
        if reason in ('OUT_OF_ORDER_BAR_TIME','DUPLICATE_BAR','CONFLICTING_BAR_REVISION') or k=='dedup':
            receipt=index['receipt',b['receipt_id'] if k=='clock_pending_result' else r['id']]
            if receipt['source']=='bar':
                raw=json.loads((root/'raw'/f"{receipt['sha256']}.bin").read_bytes())
                if 'stream' in raw:raw=raw['data']
                s=raw['s'];opened=raw['k']['t']/1000;closed=raw['k']['T']/1000;same=bar_history.get((s,opened));latest=latest_bars.get(s)
                if same:
                    expected='DUPLICATE_BAR' if (closed==same['parsed']['close'] and D(raw['k']['q'])==D(same['parsed']['quote_volume'])) else 'CONFLICTING_BAR_REVISION'
                else:expected='OUT_OF_ORDER_BAR_TIME' if latest and (opened,closed)<(latest['parsed']['open'],latest['parsed']['close']) else None
                assert expected==(reason if k!='dedup' else 'DUPLICATE_BAR'),'BAR_TERMINAL_REASON'
                if k=='dedup':assert b['original']==same['id'],'BAR_DUPLICATE_BINDING'
        if k=='event' and b['reason']=='ACCESS_RESTORED':
            old=index['input',b['previous_receipt']]['parsed'][b['symbol']];new=index['input',b['receipt_id']]['parsed'][b['symbol']]
            assert (old['status']!='TRADING' or not old['spot']) and new['status']=='TRADING' and new['spot'] is True,'EVENT_NOT_STRUCTURAL'
        if k=='decision':
            assert b['policy_hash']==sha(canonical(policy).encode()),'DECISION_POLICY'
            inputs=[index['input',i] for i in b['input_ids']]
            assert all(i['received_at']<=i.get('available_at',i['received_at'])<=b['cutoff']==b['at'] for i in inputs),'DECISION_LOOKAHEAD'
            for i in inputs:
                if i['source']=='bar':assert i['id']==latest_bars[b['symbol']]['id'],'DECISION_STALE_BAR_SELECTION'
            if b['action']=='ENTER':
                ev=index['event',b['event_id']];s=b['symbol'];m=next(i for i in inputs if i['source']=='metadata');p=next(i for i in inputs if i['source']=='mini');v=next(i for i in inputs if i['source']=='bar')
                assert ev['reason']=='ACCESS_RESTORED' and 0<=b['at']-ev['at']<=policy['event_ttl_seconds'],'ENTRY_EVENT'
                assert m['parsed'][s]['status']=='TRADING' and m['parsed'][s]['spot'] is True and b['at']-m['received_at']<=policy['metadata_fresh_seconds'],'ENTRY_METADATA'
                assert b['lot_step']==m['parsed'][s]['step'],'INSTRUMENT_STEP'
                assert fresh_at(p,s,b,policy['price_fresh_seconds'],p['parsed'][s]['source_time']) and fresh_at(v,'@bar',b,policy['bar_fresh_seconds'],v['parsed']['close']),'ENTRY_FRESHNESS'
                assert D(v['parsed']['quote_volume'])>=D(policy['minimum_1m_quote_volume']),'ENTRY_LIQUIDITY'
        if k=='outbox':assert (('event',b['event_id']) in index or ('decision',(b.get('body') or {}).get('decision_id')) in index) and b['phone_received_at'] is None,'OUTBOX_EVENT'
        if k=='fill':
            d=index['decision',b['decision_id']];receipt=index['input',b['book_receipt']];book=receipt['parsed'];s=b['symbol'];q=D(b['qty'])
            assert ('execution',d['id']) not in index,'CANCELLED_DECISION_FILLED'
            assert d['symbol']==book['symbol']==s and d['action']==b['action'],'FILL_DECISION_BINDING'
            assert d['ready_at']==d['at']+policy['minimum_execution_delay_seconds'] and d['ready_at']<=receipt['received_at']<=b['at']<=d['expires_at'],'FILL_DELAY'
            if d.get('timing'):
                bt=receipt.get('timing');dt=d['timing']
                assert bt and bt['session_id']==dt['session_id'] and bt['monotonic']>=d['ready_monotonic'],'FILL_EPOCH_DELAY'
            assert b['at']-receipt['received_at']<=policy['book_fresh_seconds'],'FILL_STALE'
            buy=b['side']=='BUY';price=D(book['ask' if buy else 'bid'])*(1+D(cfg['slippage_bps'])/10000*(1 if buy else -1))
            step=D(d['lot_step']);expected=(D(d['notional'])/price/step).to_integral_value(rounding=ROUND_DOWN)*step if buy else D(d['quantity'])
            assert q==expected and D(b['price'])==price,'FILL_PRICE_QUANTITY'
            gross=q*price;fee=gross*D(cfg['fee_rate']);assert gross==D(b['gross']) and fee==D(b['fee']),'FILL_COST'
            assert q<=D(book['ask_qty' if buy else 'bid_qty'])*D(policy['depth_participation']),'DEPTH_LIMIT'
            pos=positions.setdefault(s,[D(0),D(0)])
            if buy:cash-=gross+fee;pos[0]+=q;pos[1]+=gross+fee
            else:
                assert 0<q<=pos[0],'INVENTORY';removed=pos[1]*q/pos[0];cash+=gross-fee;realized+=gross-fee-removed;pos[0]-=q;pos[1]-=removed
            assert cash>=0,'NEGATIVE_CASH';fees+=fee;fills.append(b)
        if k=='maturity':assert not b['score_eligible'] and b['state']=='PENDING','IMMATURE_SCORE'
    expected=json.loads((root/'status.json').read_text())
    assert D(expected['cash'])==cash and D(expected['realized'])==realized and expected['paper_fills']==len(fills),'STATUS_RECONCILIATION'
    assert all(D(expected['positions'][s]['qty'])==p[0] and D(expected['positions'][s]['cost'])==p[1] for s,p in positions.items()),'POSITION_RECONCILIATION'
    return dict(status='PASS',records=len(records),paper_fills=len(fills),cash=str(cash),realized=str(realized),fees=str(fees),positions={s:[str(v) for v in p] for s,p in positions.items()},actual_delivery=False,investment_edge_validated=False)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);p.add_argument('--out',type=Path);a=p.parse_args();result=verify(a.bundle);text=json.dumps(result,indent=2)
    if a.out:a.out.write_text(text,encoding='utf-8')
    print(text)
