import hashlib
import json
import sqlite3
import heapq
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

def halt_observation(ledger,at,reason):
    key=digest([reason,int(at//86400)])
    with ledger.transaction():
        if not ledger.get('observation_halt',key):
            ledger.add('observation_halt',key,dict(id=key,at=at,reason=reason,
                gap='No further transport reads in this session',
                resume_condition='Explicit new eligible run after reconciliation; no automatic reconnect or quota reset'))

def raw_usage(ledger, now, exclude_intent=None):
    """Received bytes plus uncertain HTTP read reservations, shared with WS.

    A receipt linked by a transport result is counted once. A crashed transport
    remains charged its entire original read reservation; restart never frees it.
    """
    m=ledger._meter
    if m is None or now<m['now']:
        m=dict(seq=0,now=now,total=0,entries={},heap=[],receipts={});ledger._meter=m
    while m['heap'] and m['heap'][0][0]<now-86400:
        at,key,amount=heapq.heappop(m['heap'])
        if m['entries'].get(key)==(at,amount):m['total']-=amount;m['entries'].pop(key)
    def charge(key,at,amount):
        old=m['entries'].get(key)
        if old:m['total']-=old[1]
        if at<now-86400:
            m['entries'].pop(key,None);return
        m['entries'][key]=(at,amount)
        heapq.heappush(m['heap'],(at,key,amount))
        if at>=now-86400:m['total']+=amount
    # Incremental cache is disposable: authoritative records remain append-only.
    for seq,kind,body in ledger.db.execute('SELECT seq,kind,body FROM records WHERE seq>? ORDER BY seq',(m['seq'],)):
        m['seq']=seq
        if kind not in ('receipt','http_intent','http_result','ws_intent','ws_result'):continue
        r=json.loads(body)
        if kind.endswith('_intent'):
            group=('h:' if kind=='http_intent' else 'w:')+r['id']
            charge(group,r['at'],r.get('read_reservation',0))
        elif kind=='receipt':
            ctx=r.get('context') or {}
            group='h:'+ctx['http_intent_id'] if ctx.get('http_intent_id') else 'w:'+ctx['ws_intent_id'] if ctx.get('ws_intent_id') else 'r:'+r['id']
            m['receipts'][r['id']]=group;charge(group,r['received_at'],r['bytes'])
        else:
            group=('h:' if kind=='http_result' else 'w:')+r['id']
            old_group=m['receipts'].get(r.get('input_receipt_id'))
            if old_group and old_group!=group:charge(old_group,r['received_at'],0)
            charge(group,r['received_at'],r.get('upper_bound_charge',r.get('bytes',0)))
    # Expire after applying deltas. Stale heap entries never remove a later charge.
    while m['heap'] and m['heap'][0][0]<now-86400:
        at,key,amount=heapq.heappop(m['heap'])
        if m['entries'].get(key)==(at,amount):m['total']-=amount;m['entries'].pop(key)
    m['now']=now
    excluded=sum(m['entries'].get(prefix+str(exclude_intent),(0,0))[1] for prefix in ('h:','w:')) if exclude_intent else 0
    return m['total']-excluded

class Ledger:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if any((self.root / n).exists() for n in ('research.sqlite3', 'authority.sqlite3')):
            raise ValueError('EXISTING_RUNTIME_FORBIDDEN')
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'raw').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'observer.sqlite3', isolation_level=None, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY,kind TEXT,id TEXT,body TEXT,hash TEXT,prev TEXT,UNIQUE(kind,id))')
        self.db.execute('CREATE INDEX IF NOT EXISTS records_kind_seq ON records(kind,seq)')
        self.db.execute('CREATE TABLE IF NOT EXISTS input_index(source TEXT,symbol TEXT,seq INTEGER,PRIMARY KEY(source,symbol))')
        self.db.execute('CREATE TABLE IF NOT EXISTS bar_index(symbol TEXT,open REAL,seq INTEGER,PRIMARY KEY(symbol,open))')
        self.db.execute('CREATE TABLE IF NOT EXISTS item_index(source TEXT,symbol TEXT,seq INTEGER,PRIMARY KEY(source,symbol))')
        self.db.execute('CREATE TABLE IF NOT EXISTS clock_pending_index(id TEXT PRIMARY KEY,seq INTEGER)')
        self._meter=None;self._decoded=OrderedDict()
        # Derived indexes can be rebuilt after a copy/crash; ledger is authority.
        self.db.execute('CREATE TABLE IF NOT EXISTS projection_cursor(id INTEGER PRIMARY KEY,seq INTEGER)')
        cursor=self.db.execute('SELECT seq FROM projection_cursor WHERE id=1').fetchone()
        start=cursor[0] if cursor else 0
        for seq,kind,body in self.db.execute("SELECT seq,kind,body FROM records WHERE seq>? AND kind IN ('input','interpretation','clock_pending','clock_pending_result') ORDER BY seq",(start,)).fetchall():self._project(seq,kind,json.loads(body))
        self.db.execute('INSERT OR REPLACE INTO projection_cursor VALUES(1,?)',(self.db.execute('SELECT COALESCE(MAX(seq),0) FROM records').fetchone()[0],))
    def close(self): self.db.close()
    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            self._meter=None;self._decoded.clear()
            raise
    def get(self, kind, key):
        row = self.db.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None
    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY seq', (kind,))]
    def last(self,kind):
        row=self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY seq DESC LIMIT 1',(kind,)).fetchone()
        return json.loads(row[0]) if row else None
    def _by_seq(self,seq):
        if seq not in self._decoded:
            self._decoded[seq]=json.loads(self.db.execute('SELECT body FROM records WHERE seq=?',(seq,)).fetchone()[0])
            if len(self._decoded)>128:self._decoded.popitem(last=False)
        return self._decoded[seq]
    def latest_input(self,source,symbol='*'):
        row=self.db.execute('SELECT seq FROM input_index WHERE source=? AND symbol=?',(source,symbol)).fetchone()
        return self._by_seq(row[0]) if row else None
    def bar_input(self,symbol,opened):
        row=self.db.execute('SELECT seq FROM bar_index WHERE symbol=? AND open=?',(symbol,opened)).fetchone()
        return self._by_seq(row[0]) if row else None
    def bar_rejection(self,bar):
        same=self.bar_input(bar['symbol'],bar['open'])
        if same:return 'DUPLICATE_BAR' if same['parsed']==bar else 'CONFLICTING_BAR_REVISION'
        latest=self.latest_input('bar',bar['symbol'])
        if latest and (bar['open'],bar['close'])<(latest['parsed']['open'],latest['parsed']['close']):
            return 'OUT_OF_ORDER_BAR_TIME'
        return None
    def item_state(self,source,symbol):
        row=self.db.execute('SELECT seq FROM item_index WHERE source=? AND symbol=?',(source,symbol)).fetchone()
        if not row:return None
        value=self._by_seq(row[0])
        if 'release_id' in value:return dict(symbol=symbol,**value['time_validity'][symbol if source=='mini' else '@bar'])
        return next((i for i in value['items'] if i['symbol']==symbol),None)
    def _project(self,seq,kind,body):
        if kind=='input':
            source=body['source'];p=body['parsed']
            symbols=list(p) if source in ('mini','metadata') else [p.get('symbol')]
            advanced=[s for s in ['*']+symbols if s]
            if source=='bar':
                old=self.latest_input(source,p['symbol'])
                if old and (p['open'],p['close'])<=(old['parsed']['open'],old['parsed']['close']):advanced=[]
            self.db.executemany('INSERT OR REPLACE INTO input_index VALUES(?,?,?)',[(source,s,seq) for s in advanced])
            if source=='bar':self.db.execute('INSERT OR IGNORE INTO bar_index VALUES(?,?,?)',(p['symbol'],p['open'],seq))
            if body.get('release_id'):
                initial=self.db.execute("SELECT seq FROM records WHERE kind='interpretation' AND id=?",(body['receipt_id'],)).fetchone()[0]
                for symbol in symbols:
                    if symbol not in advanced:continue
                    current=self.db.execute('SELECT seq FROM item_index WHERE source=? AND symbol=?',(source,symbol)).fetchone()
                    value=self._by_seq(current[0]) if current else None
                    if not current or current[0]<=initial or (value.get('release_id') and value['received_at']<=body['received_at']):
                        self.db.execute('INSERT OR REPLACE INTO item_index VALUES(?,?,?)',(source,symbol,seq))
        elif kind=='clock_pending':
            self.db.execute('INSERT OR REPLACE INTO clock_pending_index VALUES(?,?)',(body['id'],seq))
        elif kind=='clock_pending_result':
            self.db.execute('DELETE FROM clock_pending_index WHERE id=?',(body['id'],))
        elif kind=='interpretation':
            for item in body['items']:
                symbol=item.get('symbol')
                if not isinstance(symbol,str) or not symbol:continue
                old=self.latest_input('bar',symbol) if body['source']=='bar' else None
                # A late, well-formed old bar must not replace the latest bar's
                # eligibility either. Preserve the interpretation in the ledger.
                if old and item.get('freshness_source_time',float('inf'))<=old['parsed']['close']:continue
                self.db.execute('INSERT OR REPLACE INTO item_index VALUES(?,?,?)',(body['source'],symbol,seq))
        self.db.execute('INSERT OR REPLACE INTO projection_cursor VALUES(1,?)',(seq,))
    def add(self, kind, key, body):
        old = self.get(kind, key)
        if old is not None:
            if encode(old) != encode(body): raise ValueError('IMMUTABLE_RECORD_CONFLICT')
            return old
        last = self.db.execute('SELECT hash FROM records ORDER BY seq DESC LIMIT 1').fetchone()
        prev = last[0] if last else '0' * 64
        h = digest([prev, kind, key, body])
        cursor=self.db.execute('INSERT INTO records(kind,id,body,hash,prev) VALUES(?,?,?,?,?)', (kind,key,encode(body),h,prev))
        if kind in ('input','interpretation','clock_pending','clock_pending_result'):self._project(cursor.lastrowid,kind,body)
        return body
    def export(self, path):
        rows = [dict(zip(('seq','kind','id','body','hash','prev'), r)) for r in self.db.execute('SELECT * FROM records ORDER BY seq')]
        Path(path).write_text('\n'.join(encode(r) for r in rows)+'\n', encoding='utf-8')
