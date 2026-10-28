"""Append-only, delayed, depth-constrained spot simulation. Never exchange orders."""
from datetime import timedelta
from .store import utc,instant
from .market import fresh_book,number

def execute_book(book,side,config,quote=None,quantity=None):
    if config["instrument"]!="SPOT_LONG":raise ValueError("DERIVATIVE_FUNDING_UNSUPPORTED")
    if side not in ("BUY","SELL"):raise ValueError("side")
    if not book["asks"] or not book["bids"]:raise ValueError("EMPTY_BOOK")
    for side_levels in (book["asks"],book["bids"]):
        for price,size in side_levels:number(price,True);number(size,True)
    if book["bids"][0][0]>=book["asks"][0][0]:raise ValueError("CROSSED_BOOK")
    if book["spread_bps"]>config["max_spread_bps"]:raise ValueError("SPREAD_LIMIT")
    age=(instant(utc())-instant(book["received_at"])).total_seconds()
    if not 0<=age<=config["max_input_age_seconds"]:raise ValueError("STALE_BOOK")
    levels=book["asks"] if side=="BUY" else book["bids"];best=levels[0][0]
    slip=config["slippage_bps_per_side"]/10000;fee=config["fee_bps_per_side"]/10000
    remaining=number(quote,True)/(1+fee) if side=="BUY" else number(quantity,True)
    got=0.;gross=0.
    for price,size in levels:
        if abs(price/best-1)*10000>config["max_book_impact_bps"]:break
        adjusted=price*(1+slip if side=="BUY" else 1-slip)
        capacity=size*config["book_participation"]
        take=min(capacity,remaining/adjusted if side=="BUY" else remaining)
        got+=take;gross+=take*adjusted
        remaining-=take*adjusted if side=="BUY" else take
        if remaining<=1e-8:break
    if remaining>1e-8 or got<=0:raise ValueError("INSUFFICIENT_VISIBLE_DEPTH")
    return dict(quantity=got,gross_usdt=gross,fee_usdt=gross*fee,
                cash_delta_usdt=-(gross*(1+fee)) if side=="BUY" else gross*(1-fee),
                average_price=gross/got,book_receipt_id=book["receipt_id"],
                spread_bps=book["spread_bps"],slippage_bps=config["slippage_bps_per_side"],
                funding_usdt=0.,funding_status="NOT_APPLICABLE_SPOT",
                hedge_error_usdt=0.,hedge_status="NO_HEDGE_SPOT_LONG",
                execution_status="BOOK_CONSTRAINED_SIMULATION_NOT_EXCHANGE_EXECUTION")

def account(store,config,policy,books=None):
    entries=[r for r in store.all("paper_entry") if r["policy"]==policy]
    exits={r["id"]:r for r in store.all("paper_exit") if r["policy"]==policy}
    open_rows=[r for r in entries if r["id"] not in exits]
    cash=config["engine_capital_krw"]["CASHFLOW"]+sum(r["cash_delta_krw"] for r in entries)+sum(r["cash_delta_krw"] for r in exits.values())
    exposure=sum(r["cost_krw"] for r in open_rows)
    equity=cash+sum((books[r["symbol"]]["mid"]*r["quantity"]*config["fx_krw_per_usdt"]
                    if books and r["symbol"] in books else r["cost_krw"]) for r in open_rows)
    return dict(cash_krw=cash,open_cost_krw=exposure,equity_krw=equity,open_positions=open_rows,
                equity_status="CURRENT_BOOK_MARK" if books and all(p["symbol"] in books for p in open_rows) else "COST_CARRY_NOT_MARKED" if open_rows else "CASH_ONLY",
                realized_pnl_krw=sum(r["realized_pnl_krw"] for r in exits.values()),
                account_type="COUNTERFACTUAL_POLICY_SLEEVE_NOT_ADDITIONAL_CAPITAL")

def maintain(store,config):
    now=utc()
    intents=[r for r in store.all("paper_intent") if not store.get("paper_entry",r["id"],True) and not store.get("paper_rejection",r["id"],True)]
    opens=[r for r in store.all("paper_entry") if not store.get("paper_exit",r["id"],True)]
    symbols={r["symbol"] for r in opens}|{r["symbol"] for r in intents if instant(r["earliest_entry"])<=instant(now)}
    books={}
    for symbol in sorted(symbols):
        try:books[symbol]=fresh_book(store,symbol)
        except Exception as exc:store.failure("paper_book",exc,symbol=symbol)
    for position in opens:
        book=books.get(position["symbol"])
        if book is None:continue
        due=instant(now)>=instant(position["exit_due"])
        stop=book["mid"]/position["average_price"]-1<=-config["max_drawdown"]
        if not due and not stop:continue
        try:
            fill=execute_book(book,"SELL",config,quantity=position["quantity"])
            cash=fill["cash_delta_usdt"]*config["fx_krw_per_usdt"]
            store.append("paper_exit",position["id"],dict(fill,id=position["id"],symbol=position["symbol"],
                         policy=position["policy"],filled_at=utc(),cash_delta_krw=cash,
                         realized_pnl_krw=cash-position["cost_krw"],reason="TIME" if due else "STOP",
                         exit_delay_seconds=max(0,(instant(utc())-instant(position["exit_due"])).total_seconds()),
                         stress_scenario="ADDITIONAL_COST_SHOCK_PER_SIDE",
                         stress_realized_pnl_krw=cash-position["cost_krw"]-position["cost_krw"]*2*config["stress_cost_per_side"]))
        except Exception as exc:store.failure("paper_exit",exc,position_id=position["id"])
    for intent in intents:
        key=intent["id"];symbol=intent["symbol"];policy=intent["policy"]
        if instant(now)<instant(intent["earliest_entry"]):continue
        reason=None;book=books.get(symbol)
        if instant(now)>instant(intent["expires_at"]):reason="ENTRY_EXPIRED"
        elif book is None:continue
        elif instant(book["received_at"])<instant(intent["earliest_entry"]):reason="PRE_DECISION_BOOK"
        state=account(store,config,policy,books)
        capital=config["engine_capital_krw"]["CASHFLOW"]
        budget=min(capital*config["risk_fraction_per_position"],config["max_symbol_krw"])
        if any(p["symbol"]==symbol for p in state["open_positions"]):reason="DUPLICATE_OPEN_SYMBOL"
        if any(p["symbol"] not in books for p in state["open_positions"]):reason="DATA_MISSING_POSITION_MARK"
        if state["cash_krw"]<budget or state["open_cost_krw"]+budget>config["max_cf_exposure_krw"]:reason="CAPITAL_LIMIT"
        if state["equity_krw"]<capital*(1-config["max_drawdown"]):reason="LOSS_LIMIT"
        # Growth owns only its separate 8m sleeve; it cannot borrow the CF sleeve.
        if config["max_cf_exposure_krw"]+config["engine_capital_krw"]["CAPITAL_GROWTH"]>config["total_capital_krw"]:reason="COMBINED_CAPITAL_LIMIT"
        if reason:
            store.append("paper_rejection",key,dict(id=key,reason=reason,at=utc(),symbol=symbol,policy=policy));continue
        try:
            fill=execute_book(book,"BUY",config,quote=budget/config["fx_krw_per_usdt"])
            filled=utc()
            store.append("paper_entry",key,dict(fill,id=key,symbol=symbol,policy=policy,filled_at=filled,
                         decision_id=key,cost_krw=-fill["cash_delta_usdt"]*config["fx_krw_per_usdt"],
                         cash_delta_krw=fill["cash_delta_usdt"]*config["fx_krw_per_usdt"],
                         exit_due=(instant(filled)+timedelta(hours=config["hold_hours"])).isoformat(),
                         delay_seconds=(instant(filled)-instant(intent["registered_at"])).total_seconds(),
                         fx_status=config["fx_status"]))
        except Exception as exc:
            store.append("paper_rejection",key,dict(id=key,reason=str(exc),at=utc(),symbol=symbol,policy=policy))
    return {p:account(store,config,p,books) for p in config["policies"]}
