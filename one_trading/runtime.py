"""Persistent forward-paper execution; consumes only fresh public observations.

A stopped Python process is a stopped agent. This module does not provision any
server and never submits an exchange order. SQLite commits state and events
atomically; process locks prevent two workers sharing the same account ledger.
"""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from .observer import (DAY,DataError,FetchError,StopRequired,PublicClient,Store,
                       integer_ms,parse_bars,select_universe,finite_number,json_text)
from .research import load_history
from .learning import HistoryPanel
from .portfolio import Book,make_plan,validate_config
from .replay import atomic_json,iso


class InstanceLock:
    def __init__(self,path:Path):self.path=path;self.handle=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.handle=self.path.open("a+b")
        self.handle.seek(0);self.handle.write(b"0");self.handle.flush();self.handle.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError("another_paper_worker_is_running") from exc
        return self
    def __exit__(self,*args):
        if self.handle:
            if os.name=="nt":
                import msvcrt
                self.handle.seek(0);msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
            self.handle.close()


class Journal:
    def __init__(self,path:Path,config:dict):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts_ms INTEGER NOT NULL,
          kind TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS no_update_event BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only');END;
          CREATE TRIGGER IF NOT EXISTS no_delete_event BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only');END;''')
        digest=hashlib.sha256(json_text(config).encode()).hexdigest()
        old=self.get("config_sha256")
        if old and old!=digest:
            self.db.close()
            raise ValueError("config_changed_use_new_workspace_to_keep_experiments_separate")
        with self.db:self.put("config_sha256",digest)
    def get(self,key,default=None):
        row=self.db.execute("SELECT v FROM state WHERE k=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def put(self,key,value):
        self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(key,json_text(value)))
    def event(self,kind,payload,ts):
        raw=json_text(payload)
        self.db.execute("INSERT INTO events(ts_ms,kind,payload,sha256) VALUES(?,?,?,?)",(ts,kind,raw,hashlib.sha256(raw.encode()).hexdigest()))
    def close(self):self.db.close()


def validate_clock(server_ms:int,now_ms:int,c:dict)->None:
    if abs(server_ms-now_ms)>c["max_clock_skew_seconds"]*1000:
        raise DataError("CLOCK_SKEW_NEW_EXECUTION_BLOCKED")


def fresh_quotes(rows:list,server_ms:int,c:dict)->dict[str,float]:
    out={}
    for row in rows:
        try:
            ts=integer_ms(row["closeTime"])
            if abs(server_ms-ts)>c["max_quote_age_seconds"]*1000:continue
            s=row["symbol"]
            if s in out:raise DataError("duplicate_quote")
            try:
                out[s]=finite_number(row["lastPrice"])
            except DataError:
                continue
        except (KeyError,TypeError):continue
    return out


class Gateway:
    def __init__(self,c:dict,workspace:Path,client=None):
        self.c=c;self.workspace=workspace;self.client=client or PublicClient()
    def fetch(self,held:list[str])->dict:
        c=self.c
        store=Store(str(self.workspace/"market.sqlite3"))
        try:
            with store.db:
                clock=self.client.get("/api/v3/time");store.snapshot(clock)
                server=integer_ms(clock.data["serverTime"])
                validate_clock(server,int(time.time()*1000),c)
                ticker=self.client.get("/api/v3/ticker/24hr");store.snapshot(ticker)
                exchange=self.client.get("/api/v3/exchangeInfo",{"permissions":"SPOT"});store.snapshot(exchange)
                selected,rejected=select_universe(exchange.data,ticker.data,server,c["universe_limit"],excluded=set(c["excluded_symbols"]))
                watched,bad=select_universe(exchange.data,ticker.data,server,
                    max(1,len(c["watchlist"])),symbols=c["watchlist"],excluded=set(c["excluded_symbols"]))
                active={r["symbol"] for r in exchange.data["symbols"] if r.get("status")=="TRADING" and r.get("quoteAsset")=="USDT"}
                if any(s not in active for s in held):
                    raise DataError("HELD_SYMBOL_NOT_TRADING_MANUAL_REVIEW_REQUIRED")
                selected=list(dict.fromkeys(["BTCUSDT"]+held+watched+selected))
                last_day=server//DAY*DAY-DAY
                last_close=last_day+DAY-1
                for s in selected:
                    params={"symbol":s,"interval":"1d","limit":c["history_days"],"endTime":last_close}
                    existing=store.db.execute("SELECT raw FROM snapshots WHERE path='/api/v3/klines' AND params=? ORDER BY id DESC LIMIT 1",(json_text(params),)).fetchone()
                    if not existing:
                        record=self.client.get("/api/v3/klines",params)
                        # Newly listed candidates may have little history, but malformed
                        # or gapped data stops the cycle rather than triggering a trade.
                        parse_bars(record.data,server,minimum=1)
                        store.snapshot(record)
                # All slow history collection precedes the fresh execution quote.
                clock2=self.client.get("/api/v3/time");store.snapshot(clock2)
                server2=integer_ms(clock2.data["serverTime"])
                if server2//DAY!=server//DAY:
                    raise DataError("UTC_DAY_ROLLED_DURING_COLLECTION_RETRY_NEXT_CYCLE")
                tick2=self.client.get("/api/v3/ticker/24hr");store.snapshot(tick2)
                validate_clock(server2,int(time.time()*1000),c)
                observed=max(server2,clock2.fetched_ms,tick2.fetched_ms,int(time.time()*1000))
                if observed//DAY!=server2//DAY:
                    raise DataError("UTC_DAY_ROLLED_AFTER_QUOTES")
                prices=fresh_quotes(tick2.data,observed,c)
                if any(s not in prices for s in held):
                    raise DataError("HELD_QUOTE_MISSING_OR_STALE")
            histories,hashes=load_history(self.workspace/"market.sqlite3")
            histories={s:b for s,b in histories.items() if s in selected}
            if "BTCUSDT" not in histories:
                raise DataError("BTC_HISTORY_MISSING")
            if any(s not in histories for s in held):
                raise DataError("HELD_HISTORY_MISSING")
            return {"server_ms":observed,"exchange_clock_ms":server2,"histories":histories,"hashes":hashes,"prices":prices,
                    "universe":selected,"unavailable_watchlist":bad,"rejected_universe":rejected,
                    "source":"BINANCE_PUBLIC_SPOT"}
        finally:store.close()


def cycle(c:dict,workspace:Path,gateway=None)->dict:
    validate_config(c)
    workspace.mkdir(parents=True,exist_ok=True)
    journal=Journal(workspace/"paper.sqlite3",c)
    try:
        book=Book.from_dict(journal.get("book",Book(c["initial_cash"]).to_dict()))
        pending=journal.get("pending",[])
        before=len(book.fills)
        snap=(gateway or Gateway(c,workspace)).fetch(list(book.positions))
        now=snap["server_ms"];prices=snap["prices"];h=snap["histories"]
        validate_clock(now,int(time.time()*1000),c)
        if not h.get("BTCUSDT") or h["BTCUSDT"][-1].close_ms != (now//DAY)*DAY-1:
            raise DataError("STALE_HISTORY_NO_EXECUTION")
        # A quote older than five minutes cannot be relabelled as a fresh fill.
        book.prices_ok(prices)
        prev=journal.get("last_prices",prices)
        if any(s not in prev for s in book.positions):prev=prices
        book.begin_day(now,prev)
        book.monitor_quotes(prices,now,c)
        execution_notes=[]
        for p in list(pending):
            if p["execute_after_ms"]<=now:
                execution_notes+=book.apply_plan(p,prices,now,c);pending.remove(p)
        day=h["BTCUSDT"][-1].open_ms
        plan=journal.get("last_plan",{})
        if day!=journal.get("signal_day"):
            panel=HistoryPanel(h,c)
            model=panel.warm_model(day)
            # Learning may be slow. Do not execute another order on this snapshot
            # afterward; the new plan is due tomorrow and will obtain a new quote.
            plan=make_plan(panel.features_on(day),model,book,prices,max(now,int(time.time()*1000)),c)
            pending.append(plan)
        output={"status":"FORWARD_PAPER_CYCLE_OK","mode":"EXPERIMENTAL_PAPER_ONLY",
                "observed_at":iso(now),"last_success_ms":now,"daily_signal_close":iso(day+DAY-1),
                "equity":book.equity(prices),"cash":book.cash,"positions":{s:asdict(p) for s,p in book.positions.items()},
                "new_simulated_fills":book.fills[before:],"total_simulated_fills":len(book.fills),
                "pending_plans":len(pending),"model":plan.get("model"),
                "execution_notes":execution_notes,"universe_count":len(snap["universe"]),
                "unavailable_watchlist":snap.get("unavailable_watchlist",{}),
                "real_orders_submitted":0,"fundamental_revaluation_evidence":"NOT_CONNECTED"}
        with journal.db:
            journal.put("book",book.to_dict());journal.put("pending",pending)
            journal.put("last_plan",plan);journal.put("signal_day",day)
            journal.put("last_prices",{s:prices[s] for s in book.positions})
            journal.put("last_success_ms",now)
            journal.event("CYCLE",output,now)
        atomic_json(workspace/"status.json",output)
        atomic_json(workspace/"latest_plan.json",plan)
        return output
    except Exception as exc:
        now=int(time.time()*1000)
        output={"status":"STOP_REQUIRED" if isinstance(exc,StopRequired) else "ERROR_NO_EXECUTION",
                "error":f"{type(exc).__name__}: {exc}","attempted_at":iso(now),
                "last_success_ms":journal.get("last_success_ms"),
                "real_orders_submitted":0,"book_state_changed":False,
                "notice":"Previous prices were not reused as current prices. No successful cycle is claimed."}
        with journal.db:journal.event("ERROR",output,now)
        atomic_json(workspace/"status.json",output)
        return output
    finally:journal.close()


def run(c:dict,workspace:Path,cycles:int=0)->int:
    count=0
    with InstanceLock(workspace/"worker.lock"):
        while True:
            begin=time.monotonic()
            result=cycle(c,workspace)
            print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
            count+=1
            if result["status"]=="STOP_REQUIRED":return 3
            if cycles and count>=cycles:
                return 0 if result["status"]=="FORWARD_PAPER_CYCLE_OK" else 2
            time.sleep(max(1,c["scan_interval_seconds"]-(time.monotonic()-begin)))
