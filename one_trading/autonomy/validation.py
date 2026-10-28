"""Shared chronological and unit boundary before any policy sees observations."""
from datetime import timedelta
from pathlib import Path
from .store import instant,utc,digest
from .market import number

def validate_packet(store,config,packet):
    cutoff=instant(packet["cutoff"]);hour=instant(packet["hour"])
    if cutoff>instant(utc()) or hour>cutoff or hour.minute or hour.second or hour.microsecond:raise ValueError("invalid_packet_clock")
    if packet["universe"]!=config["universe"] or set(packet["assets"])!=set(config["universe"]):raise ValueError("packet_universe")
    for key in packet["receipts"]:
        r=store.get("http_receipt",key)
        if not instant(r["requested_at"])<=instant(r["received_at"])<=cutoff:raise ValueError("future_or_reversed_receipt")
        if r["error"] is not None:raise ValueError("failed_receipt_in_packet")
        raw=store.root/"raw"/(r["sha256"]+".bin")
        if not raw.is_file() or __import__("hashlib").sha256(raw.read_bytes()).hexdigest()!=r["sha256"]:raise ValueError("raw_hash")
    for symbol,row in packet["assets"].items():
        if row["symbol"]!=symbol:raise ValueError("asset_identity")
        if row["price_status"]=="VALID":
            if row["bar_end_at"]!=packet["hour"]:raise ValueError("bar_hour_mismatch")
            book=row["book"]
            if instant(book["received_at"])>cutoff:raise ValueError("future_book")
            number(book["mid"],True)
            if row.get("price_unit","USDT_per_base_asset")!="USDT_per_base_asset":raise ValueError("price_unit")
            if row["numeric_status"]=="VALID":
                if row["oi_quantity_unit"]!="base_asset" or row["oi_notional_unit"]!="USDT" or row["oi_interval_hours"]!=1 or row["oi_end_at"]!=packet["hour"]:
                    raise ValueError("oi_units_or_period")
                number(row["observed_funding_interval_hours"],True)
                if instant(row["premium_exchange_time"])>cutoff+timedelta(seconds=config["max_clock_skew_seconds"]):raise ValueError("future_premium")
    return True

def source_identity():
    directory=Path(__file__).parent
    return digest({p.name:__import__("hashlib").sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob("*.py"))})


def validate_repository(config):
    import subprocess
    root=Path(__file__).resolve().parents[2]
    base=config["base_sha"]
    if len(base)!=40 or any(c not in "0123456789abcdef" for c in base):raise ValueError("invalid_base_sha")
    prefix=["git","-c","safe.directory="+str(root),"-C",str(root)]
    branch=subprocess.run(prefix+["branch","--show-current"],capture_output=True,text=True,check=True).stdout.strip()
    if branch in ("main","master",config["base_ref"]) or not branch:raise ValueError("separate_pr9_derived_branch_required")
    test=subprocess.run(prefix+["merge-base","--is-ancestor",base,"HEAD"],capture_output=True)
    if test.returncode:raise ValueError("head_not_derived_from_pr9")
    return dict(branch=branch,verified_ancestor=base)
