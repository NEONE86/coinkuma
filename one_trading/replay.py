"""Walk-forward portfolio replay over the preserved v0.2 market-data archive.

The current-survivor sample is biased. Walk-forward chronology here is an
execution/learning test, not a clean point-in-time-universe OOS certificate.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime,timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import time
from .observer import DAY,json_text
from .research import load_history
from .learning import HistoryPanel,MaturedRidge
from .portfolio import Book,make_plan,validate_config


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    temp.replace(path)


def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat() if ms is not None else None


def verify_archive(path: Path) -> dict:
    con=sqlite3.connect(f"file:{path.resolve()}?mode=ro",uri=True)
    try:
        rows=con.execute("SELECT id,sha256,raw FROM snapshots").fetchall()
        for id_,expected,raw in rows:
            if hashlib.sha256(raw).hexdigest()!=expected:
                raise ValueError(f"archive_hash_mismatch:{id_}")
        return {"snapshot_count":len(rows),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    finally:con.close()


def report_html(summary: dict, latest: dict, fills: list[dict]) -> str:
    e=html.escape
    fields=[("실행 상태","과거 재생 완료"),("총수익률",f'{summary.get("total_return",0)*100:.2f}%'),
            ("최대낙폭",f'{summary.get("max_drawdown",0)*100:.2f}%'),("모의 체결",len(fills)),
            ("모델 재학습",summary.get("model_refits",0)),("실거래", "연결하지 않음")]
    cards="".join(f'<div class="card"><small>{e(str(k))}</small><strong>{e(str(v))}</strong></div>' for k,v in fields)
    fill_rows="".join('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in (
        iso(r["ts_ms"]),r["symbol"],r["side"],round(r["notional"],2),r["reason"],
        round(r["realized_pnl"],2) if r["realized_pnl"] is not None else ""))+'</tr>' for r in fills[-40:][::-1])
    dec_rows="".join('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in (
        r["symbol"],r["state"],round(r["prediction"][0],4) if r["prediction"] else "학습 대기",
        ", ".join(r["reasons"])))+'</tr>' for r in sorted(latest.get("decisions",[]),key=lambda r: (r["prediction"] or [-99])[0],reverse=True))
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ONE Trading — 실행 증거</title><style>
    body{{background:#101722;color:#e6edf3;font:15px/1.6 system-ui,sans-serif;margin:0}}main{{max-width:1180px;margin:auto;padding:32px}}h1{{font-size:32px;margin-bottom:4px}}p,small{{color:#b4c1ce}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:#1c2938;border:1px solid #334456;border-radius:10px;padding:18px}}strong{{display:block;font-size:22px;overflow-wrap:anywhere}}.notice{{padding:16px;border-left:4px solid #d0a444;background:#25291f;margin:20px 0}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{text-align:left;border-bottom:1px solid #334456;padding:9px}}.scroll{{overflow:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#182331;padding:18px}}h2{{margin-top:32px}}</style></head><body>
    <main><h1>ONE Trading · v0.3.1</h1><p>학습형 후보 순위 → 모의 진입·추가·축소·교체 → 수수료 포함 손익</p>
    <div class="notice">이 화면은 저장된 과거 시세로 실행한 모의매매 결과다. 현재 추천·실거래·24시간 서버 가동을 의미하지 않는다. 가격 모델이며 실사용·토큰 가치귀속·언락의 자동 판단은 아직 없다.</div>
    <section class="cards">{cards}</section><h2>실행 범위</h2><p>{e(str(summary.get("price_history_start")))} ~ {e(str(summary.get("price_history_end")))}<br>{summary.get("asset_histories")}개 시계열 · {summary.get("daily_bars")}개 일봉. 초기 가격 모델의 수익성 판정: 탈락.</p><details><summary>전체 실행 수치·가정·한계 보기</summary><pre>{e(json.dumps(summary,ensure_ascii=False,indent=2))}</pre></details>
    <h2>마지막 과거 시점의 판단 · {e(str(iso(latest.get('observed_ms'))))}</h2><div class="scroll"><table><tr><th>종목</th><th>상태</th><th>28일 순로그수익 추정</th><th>근거</th></tr>{dec_rows}</table></div>
    <h2>최근 모의 체결 40건</h2><div class="scroll"><table><tr><th>UTC</th><th>종목</th><th>방향</th><th>USDT</th><th>사유</th><th>실현손익</th></tr>{fill_rows}</table></div></main></body></html>'''


def run_replay(config: dict, archive: Path, output: Path) -> dict:
    validate_config(config)
    started=time.monotonic()
    archive_evidence=verify_archive(archive)
    histories,hashes=load_history(archive)
    btc=histories.get("BTCUSDT",[])
    if len(btc)<360:raise ValueError("insufficient_archive_history")
    panel=HistoryPanel(histories,config)
    model=MaturedRidge(config)
    book=Book(config["initial_cash"])
    pending=[]; equity=[]; audits=[]; latest={}; refits=0; first_ready=None
    execution_notes=[]
    prior_prices={}
    decisions_count=Counter()
    for idx,b in enumerate(btc):
        day=b.open_ms
        bars={s:bar for s in histories if (bar:=panel.bar(s,day)) is not None}
        opens={s:x.open for s,x in bars.items()}
        closes={s:x.close for s,x in bars.items()}
        if book.positions and any(s not in bars for s in book.positions):
            raise ValueError("held_asset_history_ended_or_gapped")
        book.begin_day(day,prior_prices if prior_prices else opens)
        book.monitor_quotes(opens,day,config)
        for p in list(pending):
            if p["execute_after_ms"]<=day:
                notes=book.apply_plan(p,opens,day,config); pending.remove(p)
                if notes:
                    execution_notes.append({"ts_ms":day,"plan_id":p["id"],
                                            "observed_ms":p["observed_ms"],"notes":notes})
        book.monitor_bar(bars,b.close_ms,config)
        eq=book.equity(closes)
        book.peak=max(book.peak,eq)
        equity.append({"ts_ms":b.close_ms,"equity":eq,"cash":book.cash,
                       "positions":len(book.positions),"drawdown":eq/book.peak-1,
                       "open_risk":book.open_risk(closes,config)})
        rows=panel.matured_group(day)
        model.learn(b.close_ms,rows,b.close_ms)
        if model.fit(b.close_ms):
            refits+=1
            audits.append(model.metadata())
        fs=panel.features_on(day)
        if fs:
            # Signal closes at D end; observation D+1; earliest fill D+2 open.
            latest=make_plan(fs,model,book,closes,b.close_ms+1,config)
            pending.append(latest)
            decisions_count.update(r["state"] for r in latest["decisions"])
            if model.weights is not None and first_ready is None:
                first_ready=latest["execute_after_ms"]
        prior_prices=closes
    final_prices=prior_prices
    book.liquidate(final_prices,btc[-1].close_ms,config,"END_OF_REPLAY_LIQUIDATION")
    final_equity=book.equity(final_prices)
    equity[-1].update(equity=final_equity,cash=book.cash,positions=0,drawdown=final_equity/book.peak-1)
    sells=[r for r in book.fills if r["side"]=="SELL"]
    pnl=[r["realized_pnl"] for r in sells]
    gains=sum(v for v in pnl if v>0);losses=-sum(v for v in pnl if v<0)
    benchmark_bar=panel.bar("BTCUSDT",first_ready) if first_ready else None
    btc_net=((btc[-1].close*(1-config["slippage_rate"])*(1-config["fee_rate"])) /
             (benchmark_bar.open*(1+config["slippage_rate"])*(1+config["fee_rate"]))-1) if benchmark_bar else None
    summary={"status":"EXECUTION_REPLAY_COMPLETE_NOT_ALPHA_VALIDATED","mode":"HISTORICAL_SIMULATION",
             "price_history_start":iso(btc[0].open_ms),"price_history_end":iso(btc[-1].close_ms),
             "first_model_eligible_execution":iso(first_ready),"asset_histories":len(histories),
             "daily_bars":sum(len(v) for v in histories.values()),"initial_cash":config["initial_cash"],
             "final_cash":final_equity,"total_return":final_equity/config["initial_cash"]-1,
             "realized_pnl":book.realized_pnl,"max_drawdown":min(x["drawdown"] for x in equity),
             "fees_paid":book.total_fees,"assumed_fee_each_side":config["fee_rate"],
             "assumed_slippage_each_side":config["slippage_rate"],"fill_count":len(book.fills),
             "buy_count":sum(r["side"]=="BUY" for r in book.fills),"sell_count":len(sells),
             "sell_fill_win_rate":sum(v>0 for v in pnl)/len(pnl) if pnl else None,
             "sell_fill_profit_factor":gains/losses if losses else None,
             "win_rate_unit":"SELL_FILL_NOT_INDEPENDENT_ROUND_TRIP",
             "model_refits":refits,"latched_drawdown_halt":book.halted,
             "btc_buy_hold_same_eligible_period_net":btc_net,"cash_benchmark":0.,
             "decisions_by_state":dict(decisions_count),
             "fills_by_reason":dict(Counter(r["reason"] for r in book.fills)),
             "execution_notes_by_reason":dict(Counter(n.split(":",1)[-1]
                  for item in execution_notes for n in item["notes"])),
             "config_sha256":hashlib.sha256(json_text(config).encode()).hexdigest(),
             "archive":archive_evidence,"run_seconds":round(time.monotonic()-started,3),
             "limitations":["Current-survivor and current-liquidity selection bias; not a clean OOS universe",
                            "Fees/slippage assumed; no spread/depth, order filters, partial fills or funding",
                            "Intraday portfolio risk is checked at open/close; stop-first convention for ambiguous daily bars",
                            "28-day model markout is not the return of actual stop/rotation policy; prediction uncalibrated",
                            "Price-only features; usage, value capture, unlocks, catalysts not connected",
                            "No live exchange orders, real money, external server deployment or profitability approval"]}
    if abs(book.realized_pnl-(final_equity-config["initial_cash"]))>1e-6:
        raise AssertionError("PnL_reconciliation_failed")
    output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/"summary.json",summary)
    atomic_json(output/"replay.json",{"summary":summary,"config":config,"source_hashes":hashes,
                                    "model_audit":audits,"equity":equity,"fills":book.fills,"last_plan":latest,
                                    "execution_notes":execution_notes})
    (output/"dashboard.html").write_text(report_html(summary,latest,book.fills),encoding="utf-8")
    return summary
