"""Public, read-only Binance observations. Every response retains actual receipt time."""
import hashlib, json, math, time, uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
from .store import utc, instant, atomic_bytes, canonical

SPOT="https://data-api.binance.vision"
FUTURES="https://fapi.binance.com"
ALLOWED={
 "data-api.binance.vision":{"/api/v3/time","/api/v3/klines","/api/v3/depth","/api/v3/exchangeInfo"},
 "fapi.binance.com":{"/fapi/v1/time","/fapi/v1/premiumIndex","/fapi/v1/fundingRate","/fapi/v1/fundingInfo","/futures/data/openInterestHist"}
}
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise ValueError("redirect_not_allowed")

def number(x, positive=False):
    if isinstance(x,bool) or x is None: raise ValueError("number_required")
    v=float(x)
    if not math.isfinite(v) or (positive and v<=0): raise ValueError("invalid_number")
    return v

def ms_stamp(x):
    return datetime.fromtimestamp(number(x)/1000,timezone.utc).isoformat()

class PublicClient:
    def __init__(self,store):
        self.store=store
    def get(self,host,path,params=None):
        url=host+path+("?"
            +urlencode(params) if params else "")
        parsed=urlsplit(url)
        if parsed.scheme!="https" or path not in ALLOWED.get(parsed.hostname,set()):
            raise ValueError("public_endpoint_not_allowed")
        for cooldown in self.store.all("source_cooldown"):
            if cooldown["host"]==parsed.hostname and instant(utc())<instant(cooldown["until"]):
                raise ValueError("RATE_LIMIT_BACKOFF_UNTIL:"+cooldown["until"])
        for attempt in range(3):
            started=utc();raw=b"";error=None;status=None;retry_after=None
            try:
                req=Request(url,headers={"User-Agent":"ONE-research/1.0","Accept":"application/json"})
                with build_opener(NoRedirect()).open(req,timeout=15) as res:
                    status=res.status;raw=res.read(4*1024*1024+1)
                if len(raw)>4*1024*1024: raise ValueError("response_too_large")
                data=json.loads(raw)
                if isinstance(data,dict) and isinstance(data.get("code"),int) and data["code"]<0:
                    raise ValueError("exchange_error:"+str(data["code"]))
            except HTTPError as exc:
                status=exc.code;raw=exc.read(65536);error="HTTP_"+str(status)
                if status==429:
                    value=exc.headers.get("Retry-After","30")
                    try:retry_after=max(0,float(value))
                    except ValueError:
                        try:retry_after=max(0,(parsedate_to_datetime(value)-instant(utc())).total_seconds())
                        except Exception:retry_after=30
            except Exception as exc:
                error=type(exc).__name__+":"+str(exc)[:240]
            received=utc();sha=hashlib.sha256(raw).hexdigest();key=uuid.uuid4().hex
            atomic_bytes(self.store.root/"raw"/(sha+".bin"),raw)
            receipt=dict(id=key,url=url,requested_at=started,received_at=received,
                         http_status=status,sha256=sha,bytes=len(raw),error=error,
                         retry_after_seconds=retry_after,timestamp_unit="milliseconds where exchange supplied",
                         provenance="PUBLIC_HTTP_ACTUAL_RECEIPT",attempt=attempt+1)
            atomic_bytes(self.store.root/"receipts"/(key+".json"),canonical(receipt))
            self.store.append("http_receipt",key,receipt)
            if error is None:return data,receipt
            if status==429:
                until=(instant(received)+timedelta(seconds=retry_after or 30)).isoformat()
                self.store.append("source_cooldown",key,dict(host=parsed.hostname,until=until,receipt_id=key))
                raise ValueError("RATE_LIMIT_BACKOFF_UNTIL:"+until)
            if status in (400,401,403,404,451) or attempt==2:raise ValueError(error)
            time.sleep(min(2**attempt,4))
        raise AssertionError("unreachable")

def checked_clock(data,receipt,max_skew):
    event=instant(ms_stamp(data["serverTime"]))
    start=instant(receipt["requested_at"]);end=instant(receipt["received_at"])
    if start>end:raise ValueError("REVERSED_REQUEST_RECEIPT")
    if event<start-timedelta(seconds=max_skew) or event>end+timedelta(seconds=max_skew):
        raise ValueError("CLOCK_SKEW")
    midpoint=start+(end-start)/2
    return dict(exchange_time=event.isoformat(),
                estimated_exchange_minus_local_seconds=(event-midpoint).total_seconds(),
                roundtrip_seconds=(end-start).total_seconds(),
                exchange_time_normalized_local=midpoint.isoformat(),requested_at=receipt["requested_at"],
                received_at=receipt["received_at"],receipt_id=receipt["id"])

def closed_bars(rows,hour,received_at):
    end=int(instant(hour).timestamp()*1000)
    rows=sorted(rows,key=lambda r:r[0])
    expected=list(range(end-25*3600000,end,3600000))
    if [r[0] for r in rows]!=expected: raise ValueError("bar_gap_or_wrong_hour")
    if any(r[6]!=r[0]+3599999 or r[6]>=end or
           r[6]>int(instant(received_at).timestamp()*1000) for r in rows):
        raise ValueError("open_or_future_bar")
    for r in rows:
        for i in (1,2,3,4):number(r[i],True)
        for i in (5,7,8,9,10):
            if number(r[i])<0:raise ValueError("negative_volume")
        if number(r[10])>number(r[7])+1e-7:raise ValueError("taker_volume")
    return rows

def book_view(data,receipt):
    asks=[[number(p,True),number(q,True)] for p,q in data["asks"]]
    bids=[[number(p,True),number(q,True)] for p,q in data["bids"]]
    if not asks or not bids or asks!=sorted(asks) or bids!=sorted(bids,reverse=True):
        raise ValueError("invalid_book_order")
    if bids[0][0]>=asks[0][0]:raise ValueError("crossed_book")
    mid=(bids[0][0]+asks[0][0])/2
    return dict(asks=asks,bids=bids,mid=mid,
                spread_bps=(asks[0][0]-bids[0][0])/mid*10000,
                received_at=receipt["received_at"],exchange_time=None,
                exchange_time_status="NOT_SUPPLIED_BY_REST_DEPTH",
                last_update_id=data["lastUpdateId"],receipt_id=receipt["id"])

def derivatives_features(oi,funding,premium,adjustment,hour):
    end=int(instant(hour).timestamp()*1000)
    series={int(r["timestamp"]):r for r in oi}
    if len(series)!=len(oi):raise ValueError("duplicate_oi_period")
    prev=series[end-3600000];cur=series[end]
    q0=number(prev["sumOpenInterest"],True);q1=number(cur["sumOpenInterest"],True)
    n0=number(prev["sumOpenInterestValue"],True);n1=number(cur["sumOpenInterestValue"],True)
    history=sorted(funding,key=lambda r:r["fundingTime"])
    if len(history)<2:raise ValueError("funding_interval_missing")
    last,earlier=history[-1],history[-2]
    hours=(last["fundingTime"]-earlier["fundingTime"])/3600000
    if not 0<hours<=24:raise ValueError("invalid_funding_interval")
    rate=number(last["fundingRate"])
    if int(premium["time"])-int(last["fundingTime"])>hours*2*3600000:raise ValueError("stale_funding")
    if int(last["fundingTime"])>int(premium["time"]):raise ValueError("future_funding")
    # Last settled rate belongs to the observed historical interval, not an assumed 8h.
    interval=next((number(r["fundingIntervalHours"],True)
                   for r in adjustment if r["symbol"]==premium["symbol"]),None)
    mark=number(premium["markPrice"],True);index=number(premium["indexPrice"],True)
    return dict(oi_quantity_change=q1/q0-1,oi_notional_change=n1/n0-1,
                oi_quantity_unit="base_asset",oi_notional_unit="USDT",oi_implied_price=n1/q1,
                oi_end_at=ms_stamp(end),oi_interval_hours=1,
                oi_notional_drop_is_liquidation_evidence=False,
                last_settled_funding_rate=rate,observed_funding_interval_hours=hours,
                last_settled_funding_per_hour=rate/hours,
                announced_funding_interval_hours=interval,
                next_funding_at=ms_stamp(premium["nextFundingTime"]),
                basis=mark/index-1,mark_price=mark,index_price=index,
                premium_exchange_time=ms_stamp(premium["time"]),
                funding_status="OBSERVED_SETTLEMENT_INTERVAL")

def collect(store,config,hour):
    client=PublicClient(store);start=utc();end_ms=int(instant(hour).timestamp()*1000)
    clock_data,clock_rec=client.get(SPOT,"/api/v3/time")
    clock=checked_clock(clock_data,clock_rec,config["max_clock_skew_seconds"])
    assets={};refs=[clock_rec["id"]]
    try:
        adjustments,ar=client.get(FUTURES,"/fapi/v1/fundingInfo");refs.append(ar["id"])
    except Exception:adjustments=[]
    for symbol in config["universe"]:
        row=dict(symbol=symbol,price_status="DATA_MISSING",numeric_status="DATA_MISSING",errors=[])
        try:
            info,ir=client.get(SPOT,"/api/v3/exchangeInfo",{"symbol":symbol})
            market=info["symbols"][0]
            if market["symbol"]!=symbol or market["status"]!="TRADING" or market["quoteAsset"]!="USDT":
                raise ValueError("wrong_market")
            bars,br=client.get(SPOT,"/api/v3/klines",dict(symbol=symbol,interval="1h",
                          startTime=end_ms-25*3600000,endTime=end_ms-1,limit=25))
            bars=closed_bars(bars,hour,br["received_at"])
            depth,dr=client.get(SPOT,"/api/v3/depth",dict(symbol=symbol,limit=20))
            book=book_view(depth,dr)
            qv=number(bars[-1][7]);taker=number(bars[-1][10])
            row.update(price_status="VALID",price_unit="USDT_per_base_asset",bar_end_at=hour,
                return_1h=number(bars[-1][4],True)/number(bars[-2][4],True)-1,
                return_24h=number(bars[-1][4],True)/number(bars[-25][4],True)-1,
                quote_volume_usdt=qv,taker_buy_fraction=taker/qv if qv>0 else None,
                book=book,base_asset=market["baseAsset"],
                receipts=[ir["id"],br["id"],dr["id"]])
            refs+=row["receipts"]
        except Exception as exc:row["errors"].append(str(exc))
        try:
            oi,orr=client.get(FUTURES,"/futures/data/openInterestHist",
                     dict(symbol=symbol,period="1h",startTime=end_ms-3600000,endTime=end_ms,limit=3))
            funding,fr=client.get(FUTURES,"/fapi/v1/fundingRate",dict(symbol=symbol,limit=3))
            premium,pr=client.get(FUTURES,"/fapi/v1/premiumIndex",dict(symbol=symbol))
            if premium["symbol"]!=symbol or any(r["symbol"]!=symbol for r in oi+funding):
                raise ValueError("derivative_asset_mismatch")
            if instant(ms_stamp(premium["time"]))>instant(pr["received_at"])+timedelta(seconds=config["max_clock_skew_seconds"]):
                raise ValueError("future_premium")
            derivative=derivatives_features(oi,funding,premium,adjustments,hour)
            if row["price_status"]=="VALID" and any(not .5<v/row["book"]["mid"]<2 for v in (derivative["oi_implied_price"],derivative["mark_price"])):
                raise ValueError("derivative_quote_unit_or_market_mismatch")
            row.update(derivative)
            row["numeric_status"]="VALID";refs += [orr["id"],fr["id"],pr["id"]]
        except Exception as exc:row["errors"].append("derivatives:"+str(exc))
        assets[symbol]=row
    valid=[r for r in assets.values() if r["price_status"]=="VALID"]
    for row in valid:
        others=[r["return_1h"] for r in valid if r["symbol"]!=row["symbol"]]
        row["residual_1h"]=row["return_1h"]-sum(others)/len(others) if len(others)==len(assets)-1 and others else None
    finish=utc()
    if any(instant(r["book"]["received_at"])>instant(finish) for r in valid):raise ValueError("future_receipt")
    return dict(hour=hour,started_at=start,cutoff=finish,clock=clock,assets=assets,receipts=refs,
                universe=config["universe"],benchmark="equal_weight_other_universe_assets",
                sector_residual_status="NOT_AVAILABLE_BROAD_MARKET_PROXY_ONLY",
                event_news_status="NOT_COLLECTED_NO_NEWS_KNOWLEDGE_ASSUMED",
                oi_history_limit="Binance latest one month; gaps are not imputed",
                price_input="closed_hour_bars; contemporaneous book and derivatives overlay")

def fresh_book(store,symbol):
    data,receipt=PublicClient(store).get(SPOT,"/api/v3/depth",dict(symbol=symbol,limit=20))
    return book_view(data,receipt)

def endpoint_price(store,symbol,target):
    end=int(instant(target).timestamp()*1000)
    if end%60000:raise ValueError("endpoint_must_be_minute_boundary")
    if instant(utc())<instant(target):raise ValueError("IMMATURE")
    rows,receipt=PublicClient(store).get(SPOT,"/api/v3/klines",
             dict(symbol=symbol,interval="1m",startTime=end-60000,endTime=end-1,limit=1))
    if len(rows)!=1 or rows[0][0]!=end-60000 or rows[0][6]!=end-1:raise ValueError("endpoint_missing")
    if instant(receipt["received_at"])<instant(target):raise ValueError("future_endpoint")
    return dict(price=number(rows[0][4],True),symbol=symbol,endpoint=target,
                receipt_id=receipt["id"],received_at=receipt["received_at"])
