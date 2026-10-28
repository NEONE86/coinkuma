"""Durable records, process lock and immutable experiment configuration."""
import json, os, shutil, uuid
from contextlib import contextmanager
from pathlib import Path
from one_trading.economic_research.store import Store as BaseStore, utc, digest, canonical, atomic_bytes, instant

class Store(BaseStore):
    def __init__(self, root):
        super().__init__(root)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS ticks(id TEXT PRIMARY KEY,stage TEXT NOT NULL,updated_at TEXT NOT NULL)")
    def append(self, kind, key, data):
        old=self.get(kind,key,optional=True)
        if old is not None:
            if digest({k:v for k,v in old.items() if k!='stored_at'})!=digest(data):
                raise ValueError("immutable_payload_conflict:"+kind)
            return old
        return self.put(kind,key,dict(data,stored_at=utc()))
    def checkpoint(self, tick, stage):
        self.db.execute("INSERT INTO ticks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET stage=excluded.stage,updated_at=excluded.updated_at",(tick,stage,utc()))
    def stage(self,tick):
        row=self.db.execute("SELECT stage FROM ticks WHERE id=?",(tick,)).fetchone()
        return row[0] if row else None
    def failure(self, operation, error, **details):
        key=uuid.uuid4().hex
        self.append("failure",key,dict(operation=operation,error=str(error),**details))
    def frozen_config(self, data):
        validate_config(data)
        old=self.get("autonomy_config","v1",optional=True)
        if old and digest(old["config"])!=digest(data): raise ValueError("new_config_requires_new_workspace")
        return old or self.append("autonomy_config","v1",dict(config=data,config_hash=digest(data)))

def validate_config(c):
    if not 2<=len(c["universe"])<=50 or len(set(c["universe"]))!=len(c["universe"]): raise ValueError("universe")
    if any(not x.isalnum() or not x.endswith("USDT") for x in c["universe"]): raise ValueError("symbol")
    if c["policies"] != ["CF_BASE","CF_NUMERIC","CF_AI_MEMORY"]: raise ValueError("fixed_policies")
    if c["horizons_hours"] != [6,24,72] or c["bar_interval"]!="1h": raise ValueError("fixed_horizons")
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or v<=0 for v in [c["total_capital_krw"],*c["engine_capital_krw"].values()]): raise ValueError("positive_capital")
    if c["total_capital_krw"]>20000000 or sum(c["engine_capital_krw"].values())>c["total_capital_krw"]: raise ValueError("capital_double_count")
    if c["live_trading"] is not False or c["instrument"]!="SPOT_LONG": raise ValueError("live_or_derivative_execution_unavailable")
    if c["fx_krw_per_usdt"]<=0 or c["max_symbol_krw"]>c["engine_capital_krw"]["CASHFLOW"]: raise ValueError("capital_or_fx")
    if c["base_ref"]!="research/economic-repricing-v4-20261026" or len(c["base_sha"])!=40: raise ValueError("pr9_ref_required")
    for key in ("fee_bps_per_side","slippage_bps_per_side","signal_markout_cost_per_side","stress_cost_per_side"):
        if isinstance(c[key],bool) or not __import__("math").isfinite(c[key]) or c[key]<0: raise ValueError("invalid_cost")
    if not 0<c["book_participation"]<=1: raise ValueError("book_participation")
    if c["risk_fraction_per_position"]<=0 or c["risk_fraction_per_position"]>.1: raise ValueError("risk_limit")

@contextmanager
def process_lock(root):
    path=Path(root)/"process.lock";path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open("a+b");f.seek(0);f.write(b"0");f.flush();f.seek(0)
    try:
        if os.name=="nt":
            import msvcrt
            try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise ValueError("ALREADY_RUNNING")
        else:
            import fcntl
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise ValueError("ALREADY_RUNNING")
        yield
    finally:
        f.close() # OS releases the lock even after process termination.

def require_disk(root,minimum=32*1024*1024):
    if shutil.disk_usage(root).free<minimum: raise OSError("DISK_LOW")
