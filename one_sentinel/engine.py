"""Local vertical path: original bytes -> event -> policy -> outbox -> PAPER."""
import hashlib
import json,time,sqlite3
from contextlib import closing
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
from .store import Ledger, digest, raw_usage, halt_observation
from .observe import parse, parse_revision, PARSER_VERSION, latest, symbol_input, membership
from .clocks import capture, calibration, fresh
from .paper import account, fill
from .notify import enqueue, queue_candidate, risk_outbox
from . import pending as pending_clock

class Observer:
    def __init__(self, root, policy_path=None, account_path=None):
        base=Path(__file__).resolve().parents[1]/'configs'
        self.policy=json.loads(Path(policy_path or base/'sentinel_policy_v1.json').read_text(encoding='utf-8'))
        self.config=json.loads(Path(account_path or base/'sentinel_account_v1.json').read_text(encoding='utf-8'))
        if self.config['policy_id']!=self.policy['id'] or self.config['actual_order_capability']:raise ValueError('PAPER_POLICY_ONLY')
        self.live_approval=None
        self.current_timing=None
        source=hashlib.sha256()
        for f in sorted(Path(__file__).parent.glob('*.py')):source.update(f.name.encode());source.update(f.read_bytes())
        contract=dict(policy=self.policy,account=self.config,policy_hash=digest(self.policy),account_hash=digest(self.config),source_hash=source.hexdigest())
        dbpath=Path(root).resolve()/'observer.sqlite3'
        if dbpath.exists():
            # Reject another source/config before changing even derived tables.
            with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute("SELECT body FROM records WHERE kind='contract' AND id='v1'").fetchone()
            if row and json.loads(row[0])!=contract:raise ValueError('IMMUTABLE_RECORD_CONFLICT')
        self.l=Ledger(root)
        try:
            with self.l.transaction():
                self.l.add('contract','v1',contract)
        except BaseException:
            self.l.close();raise
    def close(self):self.l.close()
    def status(self):
        s=account(self.l,self.config)
        return dict(clock_pending=pending_clock.meter(self.l),cash=str(s['cash']),reserved=str(s['reserved']),realized=str(s['realized']),positions={k:{f:str(v) for f,v in p.items()} for k,p in s['positions'].items()},inputs=self.l.db.execute("SELECT COUNT(*) FROM records WHERE kind='input'").fetchone()[0],events=len(self.l.all('event')),paper_fills=len(self.l.all('fill')),actual_orders=0,actual_model_calls=0,actual_deliveries=sum(bool(r.get('actual_delivery')) for r in self.l.all('delivery')),maturity_pending=len(self.l.all('maturity')),outbox=len(self.l.all('outbox')))
    def event(self,symbol,reason,at,receipt,previous=None):
        key=digest([symbol,reason,receipt['id'],previous])
        e=self.l.add('event',key,dict(id=key,symbol=symbol,reason=reason,at=receipt['received_at'],noticed_at=at,receipt_id=receipt['id'],previous_receipt=previous,classification=receipt['classification']))
        enqueue(self.l,e,'BASIC_EVENT' if reason=='ACCESS_RESTORED' else 'RISK',at)
        queue_candidate(self.l,self.policy,e,'STRUCTURAL' if reason=='ACCESS_RESTORED' else 'HOLDINGS',at)
        # Indefinite until BOTH thesis/position closure and an audit release are recorded.
        self.l.add('evidence_pin',key,dict(id=key,receipt_id=receipt['id'],previous_receipt=previous,min_until=at+self.policy['horizon_seconds'],release_after_audit=False,automatic_delete=False))
        return e
    def ingest(self,source,raw,received_at,now,classification='SYNTHETIC',origin=None,context=None,timing=None,provenance=None,processing_clock=None):
        if classification not in ('SYNTHETIC','REAL_REUSED'):
            a=self.live_approval
            if classification!='REAL_FORWARD' or not a or not a['start_at']<=received_at<a['stop_at']:raise ValueError('LIVE_ADAPTER_NOT_APPROVED')
        if source not in ('mini','metadata','bar','book','clock'):raise ValueError('UNSUPPORTED_SOURCE')
        if received_at>now or received_at<0:raise ValueError('FUTURE_RECEIPT')
        if not isinstance(raw,bytes):raise TypeError('RAW_BYTES_REQUIRED')
        if len(raw)>16*1024*1024:raise ValueError('LOCAL_INPUT_SIZE_CAP')
        h=hashlib.sha256(raw).hexdigest();identity=[source,h,received_at,classification,origin,context]
        if timing is not None or provenance is not None:identity.extend([timing,provenance])
        key=digest(identity)
        path=self.l.root/'raw'/f'{h}.bin'
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=h:raise ValueError('RAW_CORRUPTION')
        http_id=(context or {}).get('http_intent_id') or (context or {}).get('ws_intent_id')
        if not self.l.get('receipt',key) and raw_usage(self.l,received_at,exclude_intent=http_id)+len(raw)>self.policy['proposed_new_trial_limits']['raw_bytes_per_day']:
            halt_observation(self.l,received_at,'RAW_DAILY_LIMIT')
            raise ValueError('RAW_DAILY_LIMIT')
        with self.l.transaction():
            if classification=='REAL_FORWARD':now=max(now,time.time())
            self.l.add('classification','run',dict(classification=classification))
            if self.l.get('receipt',key):return dict(self.l.get('receipt',key),duplicate=True)
            http_id=(context or {}).get('http_intent_id') or (context or {}).get('ws_intent_id')
            if http_id:
                intent=self.l.get('http_intent' if (context or {}).get('http_intent_id') else 'ws_intent',http_id)
                if not intent or len(raw)>intent['read_reservation']:raise ValueError('HTTP_READ_RESERVATION')
            used=raw_usage(self.l,received_at,exclude_intent=http_id)
            if used+len(raw)>self.policy['proposed_new_trial_limits']['raw_bytes_per_day']:raise ValueError('RAW_DAILY_LIMIT')
            if not path.exists():path.write_bytes(raw)
            r=dict(id=key,source=source,sha256=h,bytes=len(raw),received_at=received_at,processed_at=now,available_at=now,classification=classification,origin=origin or self.policy['sources'][source]['url'],context=context,timing=timing,provenance=provenance)
            self.l.add('receipt',key,r)
            self.current_timing=timing
            try:
                if timing is not None and timing['wall']!=received_at:raise ValueError('RECEIPT_WALL_MISMATCH')
                previous=latest(self.l,source)
                if source=='metadata' and previous and previous['origin']!=r['origin']:raise ValueError('METADATA_SCOPE_CHANGED')
                last_input=self.l.last('input')
                if last_input and now<last_input['processed_at']:raise ValueError('PROCESSING_CLOCK_REGRESSION')
                if previous and received_at<previous['received_at']:raise ValueError('OUT_OF_ORDER_RECEIPT')
                decoded=json.loads(raw)
                if isinstance(decoded,dict) and 'stream' in decoded:decoded=decoded['data']
                if source=='clock':
                    c=calibration(decoded['serverTime'],(context or {})['request_timing'],timing,key,self.policy['clock_model'])
                    self.l.add('clock_calibration',key,c)
                    if classification=='REAL_FORWARD':
                        self.current_timing=capture(processing_clock or time.time)
                        now=self.current_timing['wall']
                    pending_clock.resolve(self,now,self.current_timing,'CALIBRATION')
                    return r
                if source=='book' and 'symbol' not in decoded:decoded=dict(decoded,symbol=(context or {}).get('symbol'))
                sample=self.l.last('clock_calibration')
                data,items,times=parse_revision(source,decoded,received_at,classification,timing,sample,self.policy['clock_model'])
                if source=='mini':
                    for s in list(data):
                        old=symbol_input(self.l,'mini',s)
                        if old and data[s]['source_time']<old['parsed'][s]['source_time']:
                            del data[s];times.pop(s,None)
                            for item in items:
                                if item['symbol']==s:item.update(state='INVALID',reason='OUT_OF_ORDER_MARKET_TIME')
                        elif old and abs(D(data[s]['price'])/D(old['parsed'][s]['price'])-1)>=D('.02'):
                            self.l.add('price_move',digest([key,s]),dict(id=digest([key,s]),symbol=s,at=received_at,receipt_id=key,previous_receipt=old['id']))
                if classification=='REAL_FORWARD' and timing is not None:
                    # Features become usable after normalization, not at the
                    # earlier raw arrival. Charge processing latency to freshness
                    # and to the new decision's monotonic execution delay.
                    self.current_timing=capture(processing_clock or time.time)
                    now=max(now,self.current_timing['wall'])
                if items:
                    self.l.add('interpretation',key,dict(id=key,receipt_id=key,source=source,parser_version=PARSER_VERSION,
                        clock_version=self.policy['clock_model']['id'],calibration_id=sample['id'] if sample else None,
                        interpreted_at=now,available_at=now,items=items,accepted_symbols=sorted(data) if source=='mini' else ([data['symbol']] if data else []),provenance=provenance))
                    pending_clock.enqueue(self,r,items,sample,now)
                    if not data:raise ValueError('NO_ELIGIBLE_ITEMS')
                if source=='bar':
                    deep=membership(self.l,self.policy,now,[s for s,p in account(self.l,self.config)['positions'].items() if p['qty']])
                    if data['symbol'] not in deep:raise ValueError('NOT_IN_DEEP_SUBSCRIPTION')
                    rejection=self.l.bar_rejection(data)
                    if rejection=='DUPLICATE_BAR':
                        self.l.add('dedup',key,dict(id=key,original=self.l.bar_input(data['symbol'],data['open'])['id']));return r
                    if rejection:raise ValueError(rejection)
            except (ValueError,KeyError,TypeError,IndexError,InvalidOperation) as exc:
                self.l.add('quarantine',key,dict(id=key,error=str(exc),at=now));return r
            row=self.l.add('input',key,dict(r,available_at=now,processing_timing=self.current_timing,parsed=data,time_validity=times,parser_version=PARSER_VERSION))
            self._after_input(row,previous,now)
            return r
    def _after_input(self,row,previous,now):
        source=row['source'];data=row['parsed']
        if source=='metadata' and previous:
            old=previous['parsed']
            for s in sorted(set(old)|set(data)):
                before=old.get(s);after=data.get(s)
                if before and after and (before['status']!='TRADING' or not before['spot']) and after['status']=='TRADING' and after['spot']:
                    self.event(s,'ACCESS_RESTORED',now,row,previous['id'])
                elif before and before['status']=='TRADING' and before['spot'] and (not after or after['status']!='TRADING' or not after['spot']):
                    self.event(s,'ACCESS_INVALIDATED',now,row,previous['id'])
        open_symbols=[s for s,p in account(self.l,self.config)['positions'].items() if p['qty']]
        membership(self.l,self.policy,now,open_symbols)
        if source=='book':self.execute(row,now)
        symbols=set(open_symbols)|{e['symbol'] for e in self.l.all('event')}
        for s in sorted(symbols):self.evaluate(s,now)
    def evaluate(self,symbol,now):
        if not self.l.db.in_transaction:
            with self.l.transaction():return self.evaluate(symbol,now)
        p=self.policy;st=account(self.l,self.config);position=st['positions'].get(symbol);opened=position and position['qty']>0
        # Expiry releases reservation but never removes the original decision.
        for d in st['pending']:
            if d.get('timing') and self.current_timing and d['timing']['session_id']!=self.current_timing['session_id'] and d['action']=='EXIT':
                self.l.add('execution',d['id'],dict(id=d['id'],at=now,status='REJECTED',reason='NEW_MONOTONIC_EPOCH',book_receipt=None))
            elif now>d['expires_at']:self.l.add('execution',d['id'],dict(id=d['id'],at=now,status='REJECTED',reason='DECISION_EXPIRED',book_receipt=None))
        st=account(self.l,self.config)
        meta=latest(self.l,'metadata');pr=symbol_input(self.l,'mini',symbol);bar=symbol_input(self.l,'bar',symbol)
        events=[e for e in self.l.all('event') if e['symbol']==symbol]
        e=events[-1] if events else None
        valid=bool(meta and symbol in meta['parsed'] and meta['parsed'][symbol]['status']=='TRADING' and meta['parsed'][symbol]['spot'])
        fresh_price=fresh(pr,'mini',symbol,now,p['price_fresh_seconds'],self.current_timing)
        state=self.l.item_state('mini',symbol)
        if state and state['state']!='VALID':fresh_price=False
        fresh_meta=bool(meta and 0<=now-meta['received_at']<=p['metadata_fresh_seconds'])
        bar_state=self.l.item_state('bar',symbol)
        instrument=meta['parsed'].get(symbol,{}) if meta else {}
        lot_step=instrument.get('step')
        ret=D(pr['parsed'][symbol]['price'])*position['qty']/position['cost']-1 if opened and fresh_price else None
        total_mark=D(0)
        for asset,pos in st['positions'].items():
            if not pos['qty']:continue
            tick=symbol_input(self.l,'mini',asset)
            if not fresh(tick,'mini',asset,now,p['price_fresh_seconds'],self.current_timing):total_mark=None;break
            total_mark+=D(tick['parsed'][asset]['price'])*pos['qty']
        action='NO_ACTION';reason='REVIEWED_WAIT';quantity='0';notional='0'
        if opened:
            if (fresh_meta and not valid) or now>=position['opened']+p['horizon_seconds'] or (ret is not None and ret<=D(p['exit_return'])):
                action='EXIT';quantity=str(position['qty'])
                reason='STRUCTURAL_INVALIDATION' if fresh_meta and not valid else ('HORIZON_EXPIRED' if now>=position['opened']+p['horizon_seconds'] else 'STOP_LOSS')
            elif not fresh_price or not fresh_meta:
                action='HOLD';reason='DATA_GAP_RETAIN_INVENTORY'
            elif D(pr['parsed'][symbol]['price'])*position['qty']>D(self.config['max_symbol_mark']) or (total_mark is not None and total_mark>D(self.config['max_gross_mark'])):
                action='REDUCE';reason='MARKED_EXPOSURE_LIMIT';quantity=str((position['qty']/2//D(lot_step or self.config['quantity_step']))*D(lot_step or self.config['quantity_step']))
            elif ret<=D(p['reduce_return']) and not position['reduced']:
                action='REDUCE';reason='DRAWDOWN_LIMIT';quantity=str((position['qty']/2//D(lot_step or self.config['quantity_step']))*D(lot_step or self.config['quantity_step']))
            elif ret>=D(p['add_return']) and not position['added'] and not position['reduced'] and e and e['reason']=='ACCESS_RESTORED' and now-e['at']<=p['event_ttl_seconds']:
                action='ADD';reason='CONFIRMED_THESIS_SIZE_STEP';notional=p['add_notional']
            else:action='HOLD';reason='REVIEWED_OPEN'
        elif e and e['reason']=='ACCESS_RESTORED' and now-e['at']<=p['event_ttl_seconds'] and valid:
            consumed=any(self.l.get('decision',f['decision_id'])['event_id']==e['id'] for f in self.l.all('fill') if f['action']=='ENTER')
            if not fresh_meta or not fresh_price or (bar_state and bar_state['state']!='VALID') or not fresh(bar,'bar',symbol,now,p['bar_fresh_seconds'],self.current_timing):
                reason='DATA_INSUFFICIENT'
            elif consumed:reason='REVIEWED_THESIS_EXHAUSTED'
            elif not lot_step:reason='UNSUPPORTED_INSTRUMENT_FILTERS'
            elif D(bar['parsed']['quote_volume'])<D(p['minimum_1m_quote_volume']):reason='LIQUIDITY_INELIGIBLE'
            else:action='ENTER';reason='ACCESS_HYPOTHESIS_ELIGIBLE';notional=p['enter_notional']
        reserve=D(notional)*(1+D(self.config['fee_rate']))
        if action in ('ENTER','ADD'):
            total=sum((v['cost'] for v in st['positions'].values()),D(0));sym=position['cost'] if opened else D(0)
            if total_mark is None or total_mark+reserve+st['reserved']>D(self.config['max_gross_mark']) or reserve+st['reserved']>st['cash'] or total+st['reserved']+reserve>D(self.config['max_gross_cost']) or sym+reserve>D(self.config['max_symbol_cost']):
                action='NO_ACTION';reason='ACCOUNT_LIMIT_BLOCKED';reserve=D(0);notional='0'
        pending=[d for d in st['pending'] if d['symbol']==symbol]
        cancelled=[]
        if action in ('EXIT','REDUCE') or reason=='DATA_GAP_RETAIN_INVENTORY':
            ranks={a:i for i,a in enumerate(self.config['risk_priority'])}
            for d in pending:
                if ranks[action]<ranks[d['action']] or (action==d['action'] and (reason!=d['reason'] or quantity!=d['quantity'])):
                    self.l.add('execution',d['id'],dict(id=d['id'],at=now,status='REJECTED',reason='RISK_PRIORITY_CANCELLED',book_receipt=None))
                    self.l.add('supersession',d['id'],dict(id=d['id'],at=now,replacement_action=action,replacement_reason=reason,append_only=True))
                    cancelled.append(d['id'])
            pending=[d for d in pending if not self.l.get('execution',d['id'])]
        if pending:return pending[-1]
        inputs=[r['id'] for r in (meta,pr,bar) if r]
        key=digest([symbol,action,reason,inputs,[f['id'] for f in self.l.all('fill')],int(now//p['decision_ttl_seconds'])])
        retry=None
        if opened and action=='EXIT' and reason in p['epoch_exit_retry']['eligible_reasons'] and self.current_timing:
            rejected=[d for d in self.l.all('decision') if d['symbol']==symbol and d['action']=='EXIT' and d['at']>=position['opened']
                and d.get('timing') and d['timing']['session_id']!=self.current_timing['session_id']
                and (self.l.get('execution',d['id']) or {}).get('reason')=='NEW_MONOTONIC_EPOCH']
            if rejected:
                parent=rejected[-1];used=self.l.get('decision_retry',parent['id'])
                if used:return self.l.get('decision',used['decision_id'])
                anchor=digest([symbol,position['opened']])
                generation=1+sum(x['position_anchor']==anchor for x in self.l.all('decision_retry'))
                if generation>p['epoch_exit_retry']['max_generations_per_position']:
                    bid=digest([anchor,self.current_timing['session_id'],'EPOCH_RETRY_LIMIT'])
                    self.l.add('retry_blocked',bid,dict(id=bid,at=now,reason='EPOCH_RETRY_LIMIT',parent_decision_id=parent['id'],position_anchor=anchor))
                    return parent
                retry=dict(id=parent['id'],parent_decision_id=parent['id'],parent_decision_hash=digest(parent),
                    parent_execution_hash=digest(self.l.get('execution',parent['id'])),position_anchor=anchor,
                    generation=generation,at=now,session_id=self.current_timing['session_id'])
                key=digest(['EPOCH_EXIT_RETRY_V1',parent['id'],generation,self.current_timing['session_id'],key])
        if self.l.get('decision',key):return self.l.get('decision',key)
        d=dict(id=key,symbol=symbol,event_id=e['id'] if e else None,action=action,reason=reason,review_status='UNREVIEWED_DATA' if reason in ('DATA_INSUFFICIENT','UNSUPPORTED_INSTRUMENT_FILTERS') else 'DETERMINISTIC_REVIEWED',ai_status='UNREVIEWED_MODEL_DISABLED',at=now,cutoff=now,ready_at=now+p['minimum_execution_delay_seconds'],expires_at=now+p['decision_ttl_seconds'],input_ids=inputs,policy_hash=digest(p),notional=notional,quantity=quantity,reserve=str(reserve),lot_step=lot_step or self.config['quantity_step'],min_qty=instrument.get('min_qty','0'),min_notional=instrument.get('min_notional','0'),classification=e['classification'] if e else 'SYNTHETIC')
        if self.current_timing:
            d.update(timing=self.current_timing,ready_monotonic=self.current_timing['monotonic']+p['minimum_execution_delay_seconds'],expires_monotonic=self.current_timing['monotonic']+p['decision_ttl_seconds'])
        if retry:d.update(parent_decision_id=retry['parent_decision_id'],attempt_generation=retry['generation'])
        self.l.add('decision',key,d)
        if retry:self.l.add('decision_retry',retry['id'],dict(retry,decision_id=key))
        for previous in cancelled:self.l.add('replacement',previous,dict(id=previous,replacement_decision_id=key,at=now))
        if opened:
            cause=meta if reason=='STRUCTURAL_INVALIDATION' else pr if reason in ('STOP_LOSS','DRAWDOWN_LIMIT','MARKED_EXPOSURE_LIMIT') else None
            observed=cause.get('available_at',cause['received_at']) if cause else now
            risk_outbox(self.l,d,str(position['qty']),observed)
        return d
    def execute(self,receipt,now):
        # Recompute risk before selecting executable intents, including sell/sell
        # precedence and current inventory. A replacement still owes its own delay.
        self.evaluate(receipt['parsed']['symbol'],now)
        for d in account(self.l,self.config)['pending']:
            if d['symbol']!=receipt['parsed']['symbol']:continue
            if d['action'] in ('ENTER','ADD'):
                m=latest(self.l,'metadata');pr=symbol_input(self.l,'mini',d['symbol']);e=self.l.get('event',d['event_id'])
                ok=m and d['symbol'] in m['parsed'] and m['parsed'][d['symbol']]['status']=='TRADING' and m['parsed'][d['symbol']]['spot'] and now-m['received_at']<=self.policy['metadata_fresh_seconds'] and fresh(pr,'mini',d['symbol'],now,self.policy['price_fresh_seconds'],self.current_timing) and now-e['at']<=self.policy['event_ttl_seconds']
                if ok and d['action']=='ADD':
                    pos=account(self.l,self.config)['positions'].get(d['symbol'])
                    ok=bool(pos and pos['qty'] and D(pr['parsed'][d['symbol']]['price'])*pos['qty']/pos['cost']-1>D(self.policy['exit_return']))
                if not ok:
                    self.l.add('execution',d['id'],dict(id=d['id'],at=now,status='REJECTED',reason='BUY_INPUT_INVALIDATED',book_receipt=receipt['id']));continue
            fill(self.l,self.policy,self.config,d,receipt,now,self.current_timing)
    def tick(self,now,timing=None):
        self.current_timing=timing
        with self.l.transaction():
            pending_clock.resolve(self,now,timing,'TICK')
            if self.current_timing:now=max(now,self.current_timing['wall'])
            for s in sorted({e['symbol'] for e in self.l.all('event')}):self.evaluate(s,now)
    def export(self,destination):
        from .accounting import snapshot
        destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
        self.l.export(destination/'ledger.jsonl')
        (destination/'status.json').write_text(json.dumps(self.status(),indent=2),encoding='utf-8')
        (destination/'meter.json').write_text(json.dumps(snapshot(self.l),indent=2),encoding='utf-8')
