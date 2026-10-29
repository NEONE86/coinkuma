"""Actual public observations, including coverage of the forced-flow source."""
import hashlib
import json
import math
import statistics
import time
import uuid
from datetime import timedelta
from one_trading.autonomy.store import utc, instant, atomic_bytes
from one_trading.autonomy.market import PublicClient, SPOT, fresh_book, number, checked_clock

FLOW_URL = "wss://stream.bybit.com/v5/public/linear"

def normalize_flow(message, received_at, universe, max_delay=10):
    rows = []
    for i, x in enumerate(message.get("data", [])):
        if x.get("s") not in universe or message.get("topic") != "allLiquidation." + x["s"]:
            raise ValueError("FLOW_SYMBOL_TOPIC_MISMATCH")
        if x.get("S") not in ("Buy", "Sell"):
            raise ValueError("FLOW_SIDE")
        at = instant(received_at).fromtimestamp(number(x["T"])/1000, instant(received_at).tzinfo)
        delay = (instant(received_at)-at).total_seconds()
        if not -3 <= delay <= max_delay:
            raise ValueError("FLOW_CLOCK_OR_DELAY")
        # Bybit S is POSITION side. Buy means long liquidated -> forced SELL.
        rows.append(dict(symbol=x["s"], event_at=at.isoformat(), received_at=received_at,
                         forced_side="SELL" if x["S"] == "Buy" else "BUY",
                         quantity_base=number(x["v"], True), bankruptcy_price=number(x["p"], True),
                         price_is_executable=False, item_index=i, claim_type="SOURCE_CLAIM",
                         constraint="VENUE_LIQUIDATION", unit="BASE_ASSET"))
    return rows

def capture_flow(store, config, key):
    old = store.get("flow_window", key, True)
    if old:
        return old
    from websockets.sync.client import connect
    start = utc(); began = time.monotonic(); frames=[]; rows=[]; error=None
    acknowledged=False; pong=None; sent_ping=0; seen=set(); duplicates=0
    try:
        with connect(FLOW_URL, open_timeout=15, close_timeout=2, ping_interval=20,
                     max_size=1048576) as ws:
            ws.send(json.dumps({"op":"subscribe", "args":["allLiquidation."+s for s in config["universe"]]}))
            deadline=time.monotonic()+config["stream_seconds"]
            while time.monotonic()<deadline:
                if time.monotonic()-sent_ping>=20:
                    ws.send('{"op":"ping"}'); sent_ping=time.monotonic()
                try:
                    raw=ws.recv(timeout=min(5,max(.01,deadline-time.monotonic())))
                except TimeoutError:
                    continue
                received=utc(); raw=raw.encode() if isinstance(raw,str) else raw
                sha=hashlib.sha256(raw).hexdigest(); data=json.loads(raw)
                atomic_bytes(store.root/"raw"/(sha+".bin"),raw)
                fid=uuid.uuid4().hex
                store.append("flow_frame",fid,dict(id=fid,window_id=key,url=FLOW_URL,
                     received_at=received,sha256=sha,bytes=len(raw)))
                frames.append(fid)
                if data.get("op")=="subscribe":
                    if data.get("success") is not True: raise ValueError("SUBSCRIPTION_REJECTED")
                    acknowledged=True
                if data.get("op")=="pong" or data.get("ret_msg")=="pong": pong=received
                if data.get("topic", "").startswith("allLiquidation."):
                    if sha in seen:
                        duplicates+=1;continue
                    seen.add(sha)
                    for row in normalize_flow(data,received,config["universe"],config["max_event_delay_seconds"]):
                        row["frame_id"]=fid;row["raw_hash"]=sha;rows.append(row)
    except Exception as exc:
        error=type(exc).__name__+":"+str(exc)[:500]
    end=utc();elapsed=time.monotonic()-began
    complete=bool(acknowledged and error is None and duplicates==0 and
                  elapsed>=config["stream_seconds"] and pong and
                  (instant(end)-instant(pong)).total_seconds()<40)
    return store.append("flow_window",key,dict(id=key,url=FLOW_URL,started_at=start,completed_at=end,
          frame_ids=frames,events=rows,error=error,subscription_acknowledged=acknowledged,
          last_pong_at=pong,duplicate_frames=duplicates,elapsed_seconds=elapsed,
          coverage="CONTIGUOUS_SUBSCRIBED_WINDOW" if complete else "INCOMPLETE",
          coverage_scope="BYBIT_PUBLISHER_STREAM_ONLY_NOT_AGGREGATE_MARKET_OR_INDEPENDENT_VENUE_AUDIT"))

def fit_exposures(series, universe, fit_end_ms, n):
    expected=list(range(fit_end_ms-(n+1)*3600000,fit_end_ms,3600000))
    closes={}
    for symbol in universe:
        rows=series[symbol]
        if [r[0] for r in rows]!=expected or any(r[6]!=r[0]+3599999 for r in rows):
            raise ValueError("FIT_BAR_GAP_OR_FUTURE")
        closes[symbol]=[number(r[4],True) for r in rows]
    returns={s:[math.log(b/a) for a,b in zip(v,v[1:])] for s,v in closes.items()}
    fits={}
    for symbol in universe:
        other=[s for s in universe if s!=symbol]
        x=[sum(returns[s][i] for s in other)/len(other) for i in range(n)]
        y=returns[symbol];mx=statistics.mean(x);my=statistics.mean(y)
        var=sum((v-mx)**2 for v in x)
        if var<=1e-14:raise ValueError("FIT_BENCHMARK_VARIANCE")
        beta=sum((a-mx)*(b-my) for a,b in zip(x,y))/var;alpha=my-beta*mx
        sigma=math.sqrt(sum((b-alpha-beta*a)**2 for a,b in zip(x,y))/(n-2))
        if sigma<=1e-9:raise ValueError("FIT_RESIDUAL_VARIANCE")
        fits[symbol]=dict(beta=beta,alpha_hourly=alpha,sigma_hourly=sigma,
                          anchor_price=closes[symbol][-1],benchmark_symbols=other,
                          fit_end_ms=fit_end_ms,observations=n)
    return fits

def state_at(fits,books,at):
    state={}
    for symbol,fit in fits.items():
        other=fit["benchmark_symbols"]
        if symbol not in books or any(s not in books for s in other):continue
        hours=(instant(at).timestamp()*1000-fit["fit_end_ms"])/3600000
        if hours<0:raise ValueError("BEFORE_FIT_END")
        benchmark=sum(math.log(books[s]["mid"]/fits[s]["anchor_price"]) for s in other)/len(other)
        reference=fit["anchor_price"]*math.exp(fit["beta"]*benchmark+fit["alpha_hourly"]*hours)
        residual=math.log(books[symbol]["mid"]/reference)
        sigma=fit["sigma_hourly"]*math.sqrt(max(hours,1/60))
        state[symbol]=dict(reference_price=reference,residual=residual,z=residual/sigma,
                           sigma=sigma,mid=books[symbol]["mid"],spread_bps=books[symbol]["spread_bps"])
    return state

def collect_snapshot(store,config,key,flow):
    old=store.get("alpha_snapshot",key,True)
    if old:return old
    client=PublicClient(store);clock,r=client.get(SPOT,"/api/v3/time")
    clock=checked_clock(clock,r,3)
    earliest=min([e["event_at"] for e in flow["events"]]+[flow["started_at"]])
    fit_end=int(instant(earliest).timestamp()//3600)*3600000
    series={};receipts={};books={};errors={}
    info,info_receipt=client.get(SPOT,"/api/v3/exchangeInfo",{"symbols":json.dumps(config["universe"],separators=(',',':'))})
    members={x['symbol']:x for x in info['symbols']}
    for s in config["universe"]:
        try:
            if members[s]["status"]!="TRADING" or members[s].get("isSpotTradingAllowed") is not True:
                raise ValueError("UNAVAILABLE_SPOT_SYMBOL")
            rows,receipt=client.get(SPOT,"/api/v3/klines",dict(symbol=s,interval="1h",
                startTime=fit_end-(config["fit_hours"]+1)*3600000,endTime=fit_end-1,limit=config["fit_hours"]+1))
            series[s]=rows;receipts[s]=receipt["id"];books[s]=fresh_book(store,s)
        except Exception as exc:errors[s]=str(exc)
    cutoff=utc();fits={};state={}
    if not errors:
        fits=fit_exposures(series,config["universe"],fit_end,config["fit_hours"])
        state=state_at(fits,books,cutoff)
    return store.append("alpha_snapshot",key,dict(id=key,cutoff=cutoff,universe=config["universe"],
         fit_end_ms=fit_end,fit_receipts=receipts,fits=fits,books=books,state=state,errors=errors,
         filters={s:members[s]['filters'] for s in config['universe'] if s in members},
         exchange_info_receipt=info_receipt["id"],clock=clock,flow_window_id=flow["id"]))

def economic_label(initial, final, symbol, side, now, due, ratio=.5):
    if instant(now)<instant(due):return "PENDING"
    if final is None or any(w["coverage"]!="CONTIGUOUS_SUBSCRIBED_WINDOW" for w in (initial,final)):
        return "UNKNOWN"
    if instant(final["started_at"])<instant(due):raise ValueError("EARLY_ECONOMIC_WINDOW")
    totals=[sum(e["quantity_base"] for e in w["events"] if e["symbol"]==symbol and e["forced_side"]==side) for w in (initial,final)]
    if totals[0]<=0:return "UNKNOWN"
    return "TRUE" if totals[1]<=ratio*totals[0] else "FALSE"
