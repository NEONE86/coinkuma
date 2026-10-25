"""Publish reviewed aggregates only; raw prices and ledgers stay outside Git."""
from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.repricing_v3 import STRATEGIES, ERAS
from scripts.run_repricing_v3 import file_sha


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def csv_write(path,rows):
    if not rows:
        return
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=keys,lineterminator='\n')
        w.writeheader();w.writerows(rows)


def pct(v):
    return '—' if v is None else f'{v*100:.2f}%'


def num(v):
    return '—' if v is None else f'{v:.3f}'


def interval(v, percent=True):
    f=pct if percent else num
    return '추정 불가' if v is None else f'[{f(v[0])}, {f(v[1])}]'


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(map(str,r))+' |' for r in rows])+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=True)
    comparison=read(a.run/'comparison.json');atlas=read(a.run/'atlas_summary.json')
    stats=read(a.run/'atlas_statistics.json');analysis=read(a.run/'account_analysis.json')
    review=read(a.run/'independent_review.json');source=read(a.run/'source_audit/data_audit.json')
    manifest=read(a.run/'run_manifest.json')
    if not review['passed']:
        raise AssertionError('do_not_publish_unverified_results')
    next_action='공개·최초 관측·실행 시각과 원문 해시를 갖춘 토큰 가치귀속 사건 패널 하나를 구축한다.'
    names={'demand':'거래대금·상대강도','compression':'수축 후 돌파','price_control':'가격 대조군'}
    intro='# ONE Trading — Cashflow Alpha v3: Repricing Opportunity Atlas\n\n'
    edge=any(v['research_decision']=='RESEARCH_EDGE' for v in analysis['decisions'].values())
    intro+=f'**검증된 우위:** {"사전 기준 통과 가설 있음 — 아래 판정 참조" if edge else "없음. 검증·도전 구간에서 가격 대조군 대비 계좌 수준 우위를 확정하지 못했다."}\n\n'
    intro+='**실패한 가설:** '+', '.join(names[s]+' '+v['decision'] for s,v in analysis['decisions'].items())+'.\n\n'
    intro+='**다음 단 하나의 행동:** '+next_action+'\n\n'
    intro+='실거래 적격은 **없음**. 신규 진입 때의 합산위험3% 제한과 달리, 스트레스 연속 계좌의 보유 중 종가 총위험은 최대12.09%까지 증가했다. 과거 구간은 이미 다른 연구에 사용되었고 완전히 미관측된 OOS가 아니다. 원시 가격·개별 원장은 로컬에 보존하며 이 PR은 집계와 재현 코드를 제공한다.\n\n'
    result=intro+'## 스트레스 비용, 연속 계좌\n\n'
    rows=[]
    for s in STRATEGIES:
        x=comparison[s]['stress']['continuous'];d=analysis['accounts'][f'{s}__stress__continuous']
        rows.append([names[s],pct(x['total_return']),pct(x['cagr']),num(x['profit_factor']),
            f"{x['expectancy_per_closed_trade']*2000:,.0f}원" if x['expectancy_per_closed_trade'] is not None else '—',
            pct(x['max_drawdown']),x['trade_count'],d['longest_losing_campaign_streak'],pct(x['average_daily_exposure'])])
    result+=table(['규칙','누적수익','CAGR','PF','거래당 기대손익¹','MDD','거래','연속손실','평균 투입률'],rows)
    result+='\n¹ 최초 10,000 가상 USDT = 2,000만 원 고정 환산. 환율·세금·출금 미반영, 급여나 실현 가능한 현금수입을 뜻하지 않는다. PF와 거래수는 추가매수 없는 완결 캠페인 기준이다. 일별 종가 MDD와 장중 동시저가 손실 경계는 다르다.\n\n'
    rows=[]
    for s in STRATEGIES:
        for era in ERAS:
            x=comparison[s]['stress'][era]
            rows.append([names[s],era,pct(x['total_return']),pct(x['cagr']),num(x['profit_factor']),x['trade_count'],pct(x['max_drawdown']),pct(x['average_daily_exposure'])])
    result+='## 독립 구간 계좌\n\n'+table(['규칙','구간','수익','CAGR','PF','거래','MDD','평균 투입률'],rows)
    result+='\n개발 2019–2022, 검증 2023–2024, 도전 2025–2026-09. 마지막 신호의 D+2 진입 후 180일 경로까지 각 구간 안에 완성되게 경계 181일을 제외한다. 전체 마지막 일별 적격 신호는 2026-04-02, 주간 진입 신호는 2026-03-29이다. 최근 상승 성공 사례만 먼저 포함하지 않았다. 재설정 계좌를 연결해 수익곡선을 만들지 않았다.\n\n'
    rows=[]
    for s in ('demand','compression'):
        for fold in ('continuous','validation','challenge'):
            v=analysis['paired_controls'][s][fold]
            d=analysis['accounts'][f'{s}__stress__{fold}']
            rows.append([names[s],fold,pct(v['mean_monthly_advantage']),interval(v['interval_97_5pct']),
                         interval(d['trade_uncertainty']['expectancy_95pct'],False),interval(d['trade_uncertainty']['pf_95pct'],False)])
    result+='## 대조군 우위와 불확실성\n\n'+table(['규칙','구간','평균 월수익 차이','차이 97.5% 구간','기대손익 95% 구간²','PF 95% 구간'],rows)
    result+='\n² 가상 USDT/완결 거래. 달력 6개월 블록을 함께 재표집하고, 거래는 진입 반기별 캠페인 묶음으로 재표집한다(2,000회, 고정 시드). 두 가설 비교에는 97.5% 구간을 적용했다. 도전 구간의 블록 수는 적고, 구조 변화·과거 연구 선택 편향까지 보정하지 못한다. 이 구간은 미래 성공확률이 아니다.\n\n'
    rows=[]
    for s in STRATEGIES:
        d=analysis['accounts'][f'{s}__stress__continuous'];t=d['targets'];n=d['full_months']
        rows.append([names[s],f"{t['fixed20m_krw']['5000000']}/{n}",f"{t['fixed20m_krw']['10000000']}/{n}",
                     f"{t['compounded_account_krw']['5000000']}/{n}",f"{t['compounded_account_krw']['10000000']}/{n}",
                     f"{t['realized_krw']['5000000']}/{n}",f"{t['realized_krw']['10000000']}/{n}"])
    result+='## 월 500만·1,000만 원 목표\n\n'+table(['규칙','고정원금 500만','고정원금 1,000만','복리계좌 500만','복리계좌 1,000만','실현손익 500만','실현손익 1,000만'],rows)
    result+='\n유휴 월 포함. 월별 상세와 기본 비용 결과도 [monthly_krw.csv](monthly_krw.csv)에 있다. 고정원금 환산은 월수익률×2,000만 원, 복리계좌는 계좌평가액 차이, 실현손익은 매도·상각 원장 합계다. 평가손익을 현금 출금으로 혼동하지 않는다.\n\n'
    rows=[]
    for s in STRATEGIES:
        x=comparison[s]['stress']['continuous'];d=analysis['accounts'][f'{s}__stress__continuous']
        rows.append([names[s],pct(comparison[s]['zero']['continuous']['total_return']),pct(comparison[s]['base']['continuous']['total_return']),pct(x['total_return']),
                     pct(comparison[s]['delay3_stress']['continuous']['total_return']),pct(d['top5_winners_fraction_of_gross_profit']),pct(d['return_ex_top5_winners']),pct(d['max_close_open_risk'])])
    result+='## 실패 원인과 손익 편향\n\n'+table(['규칙','무비용 D+2','기본 D+2','스트레스 D+2','스트레스 D+3','상위 5승 비중','상위 5승 제외 수익','최대 종가 총위험'],rows)
    result+='\n예측력은 독립 신호의 성공률과 가격 대조군 대비 월수익 차이로, 오탐은 실패 신호 비율과 손절 선행률로 구분했다. 비용은 같은 체결의 수수료·슬리피지 합계 및 무비용 재실행, 지연은 D+2와 더 늦은 D+3으로 비교한다. D+1 체결은 실행하지 않았다. 이 진단은 상호작용이 있어 합산 가능한 인과 분해가 아니다.\n\n'
    for s in STRATEGIES:
        x=comparison[s]['stress']['continuous'];d=analysis['accounts'][f'{s}__stress__continuous']
        result+=f"- **{names[s]}:** 후보 {x['signal_funnel']['candidate_signal_count']}개, 슬롯 거절 {x['signal_funnel']['capacity_rejections']}개, 종료 거래 {x['trade_count']}개. 평균 자본 투입 {pct(x['average_daily_exposure'])}, 수수료 {x['fees_paid']:.2f}, 슬리피지 {x['slippage_cost']:.2f} 가상 USDT. 상각 {x['writeoff_count']}건/{x['writeoff_cost_basis']:.2f}, 영구 중단 {x['latched_drawdown_halt']}, 장중 동시저가 손실 경계 {pct(x['intraday_low_drawdown_bound'])}.\n"
    result+='\n신규 진입 위험은 0.5%/종목·합계3%를 재계산한다. 보유자산 가격변화와 갭 때문에 이후 위험이 3%보다 커질 수 있다. 고정15% 초기 손절 때문에 종목당 목표 비중은 약3.33%이며 비용·동시위험 제한이 이를 더 줄인다. 자본 투입 제한을 레버리지로 우회하지 않았다.\n\n'
    result+='## 판정과 자료\n\n'
    for s,v in analysis['decisions'].items():
        result+=f"- {names[s]}: 연구 판정 **{v['research_decision']}**, 자본성장·실거래 관련 판정 **{v['decision']}**. 실패 연구 기준: {', '.join(v['failed_research_gates'])}.\n"
    result+='\n[OPPORTUNITY_ATLAS](OPPORTUNITY_ATLAS.md) · [MATCHED_CONTROLS](MATCHED_CONTROLS.md) · [PROTOCOL](PROTOCOL.md) · [INDEPENDENT_REVIEW](INDEPENDENT_REVIEW.md) · [NEXT_DECISION](NEXT_DECISION.md) · [근본 증거](../../REPRICING_V3_FUNDAMENTALS.md). 실주문·실계좌·유료 데이터·PR 병합·레버리지는 실행하지 않았다.\n'
    (out/'RESULTS.md').write_text(result,encoding='utf-8')
    (out/'README.md').write_text(intro+'[전체 결과](RESULTS.md) · [아틀라스](OPPORTUNITY_ATLAS.md) · [대조군](MATCHED_CONTROLS.md) · [프로토콜](PROTOCOL.md) · [검증](INDEPENDENT_REVIEW.md) · [다음 행동](NEXT_DECISION.md)\n',encoding='utf-8')
    # Full daily counts are descriptive, never represented as independent trials.
    text='# OPPORTUNITY_ATLAS\n\n'+f"원본 {source['collection']['markets']}개 시장·{source['collection']['daily_bars']:,}개 일봉. 적격 일별 관측 {atlas['counts']['eligible_daily_rows']:,}개, 미성숙 제외 {atlas['counts']['immature_eligible_symbol_dates_excluded']:,}개, 구간 경계 제외 {atlas['counts']['boundary_purged_daily_rows']:,}개. 추론용 비중복 앵커 {atlas['anchors']:,}개, 가설별 비중복 신호 {atlas['episodes']:,}개.\n\n"
    text+='**전체 Binance 시장이라는 표현의 한계:** 기존 아카이브에 들어 있는 USDT 현물 756개 이력을 전수 재사용했다. 다른 호가통화·선물·완전한 과거 상장 공지를 포괄하지 않는다. BTC는 국면 기준이고, 안정화폐·법정통화·레버리지 토큰은 상속한 제외 규칙을 적용한다. 240일 연속 이력·30일 평균 거래대금100만USDT 기준 이전의 신규 시장은 적격이 아니다. 미래 생존 여부로 걸러내지 않았다.\n\n'
    text+='## 모든 적격 일별 관측의 기술통계\n\n'
    text+=table(['구간','목표','관측수','도달','비율','D+2 도달','그중 손절 선행','경로 종료'],[
        [r['era'],r['label'],r['n'],r['hits'],pct(r['hits']/r['n']),r['delayed_hits'],r['stop_preceded_delayed_hit'],r['ended_paths']]
        for r in atlas['daily_descriptive'] if r['era']!='all'])
    text+='\n위 행들은 중첩된 관측이므로 독립 거래수나 검정 표본수로 사용하지 않는다. 가격 목표는 D종가 기준이며 미래 고가를 연구 라벨에만 사용한다. 손절 선행은 D+2 시가를 기준으로 목표를 다시 측정하고 D종가의85% 고정 손절을 적용한 진단이다. 실제 계좌에는 비용·갭 거절·주간 트레일·부분 익절·시간청산도 적용된다.\n\n'
    text+='## 비중복 신호의 도달시간·최대 역행폭\n\n'
    text+=table(['규칙','구간','목표','신호','도달','중앙 도달일','성공 중앙 MAE','D+2 도달','손절 선행'],[
        [names[r['strategy']],r['era'],r['label'],r['n'],r['hits'],r['median_days'] if r['median_days'] is not None else '—',pct(r['median_mae']),r['delayed_hits'],r['delayed_stop_preceded']]
        for r in stats['episode_summary']])
    text+='\n목표 도달일의 저가도 MAE에 포함하므로 장중 선후관계가 모호하면 불리하게 취급한다. 처음 없는 일봉이나 확인된 LUNA 동일성 경계를 만나면 회수액0으로 경로를 종료하되, 이전 목표 도달 사실은 보존한다. `ENDS_BEFORE_DATASET_END`는 마지막 관측이 일찍 끝났다는 뜻이며, 공식 상장폐지를 확정하는 상태가 아니다.\n\n'
    text+='시장별 [atlas_market_summary.csv](atlas_market_summary.csv), 국면별 [regime_anchors.csv](regime_anchors.csv), 전체 신호 통계 [signal_episodes_summary.csv](signal_episodes_summary.csv), [원본 감사](data_audit.json)를 제공한다. 행별 아틀라스와 매칭 상세는 로컬 압축 산출물에 보존하며 코드로 재생성한다.\n'
    (out/'OPPORTUNITY_ATLAS.md').write_text(text,encoding='utf-8')
    text='# MATCHED_CONTROLS\n\n'
    text+=f"180일2배에 성공한 비중복 앵커 {atlas['matching']['cases']}개 중 {atlas['matching']['matched_cases']}개에 실패 대조군 {atlas['matching']['control_rows']}개를 연결했다. 같은 UTC 날짜, 거래대금 구간, 연속 이력 구간 안에서 최대3개를 선택하고 날짜 내 재사용을 금지했다. 미매칭 성공도 버리지 않는다.\n\n"
    text+='유동성 구간은 100만–300만–1,000만–3,000만–1억–무한 USDT/일, 이력은240–365–730–무한 일이다. 같은 구간 안에서 로그 거래대금 거리+로그 이력 거리 순, 동률은 심볼 순이다. 정확히 같은 거래대금 수치라는 뜻은 아니다. 실패는 해당180일2배 라벨 미도달이며, 다른 기간·3배 목표의 실패 대조군이라고 확대 해석하지 않는다. 미래 결과는 이 기술적 매칭에만 사용했다.\n\n'
    text+='실패 사례를 뽑은 매칭 패널로 성공 기본확률을 계산하면 안 된다. 아래 확률은 **결과를 보지 않고 정한 전체 비중복 앵커**에서 계산한다. FPR=실패 중 신호 비율, FDR=신호 중 실패 비율로 분모를 구분한다.\n\n'
    text+=table(['구간','신호','앵커/신호','기본확률','조건부확률 [95%]','FPR','FDR'],[
        [r['era'],r['feature'],f"{r['n']}/{r['signals']}",pct(r['base_probability']),pct(r['conditional_probability'])+' '+interval(r['conditional_probability_95pct']),pct(r['false_positive_rate']),pct(r['false_discovery_rate'])]
        for r in stats['rates'] if r['label']=='180d_2x'])
    text+='\n시간 반기 군집과 심볼 군집을 각각2,000회 재표집한 구간의 합집합을 표시했다. 두 군집축을 완전히 동시에 모델링한 신뢰구간은 아니며, 적은 국면 수와 잔여 종목 상관을 해결하지 못한다. 매칭된 비교는 인과 효과가 아니고, 사례·대조군의 반복 사용이 여러 날짜에 걸쳐 독립성을 만들지 않는다.\n\n'
    text+='**희소 표본의 한계:** 사전 고정된 앵커 날짜에는 compression 신호가0개이므로 조건부확률을 추정하지 못한다. 실제 가설별 사건은 별도로 아틀라스에 모두 보고하고, 유리한 신호를 잡으려고 앵커를 사후 변경하지 않았다. 성공0건 또는 전부 성공인 셀에서 비모수 재표집 구간은 [0,0] 또는 [1,1]로 퇴화할 수 있다. 이는 실제 성공확률이0 또는1이라는 확신을 뜻하지 않으며, 해당 경계 구간은 불확실성의 유효한 상한·하한으로 사용하지 않는다. 연구 우위 판정은 이 확률표가 아닌 고정된 계좌·대조군 게이트를 따른다.\n\n모든90/180일2/3배 확률·불확실성은 [conditional_rates.json](conditional_rates.json), 매칭 특징 차이는 [matched_differences.csv](matched_differences.csv), 사례별 대응은 [matched_pairs.csv](matched_pairs.csv)에 있다.\n'
    (out/'MATCHED_CONTROLS.md').write_text(text,encoding='utf-8')
    protocol='# PROTOCOL\n\n'+(ROOT/'docs/REPRICING_V3_PROTOCOL.json').read_text(encoding='utf-8')
    # Keep the protocol accessible as prose plus exact linked JSON.
    protocol='# PROTOCOL\n\n사전 고정 원문은 [protocol.json](protocol.json), 코드 해시는 [freeze.json](freeze.json)이다.\n\n'
    protocol+='로컬 사전 규칙 커밋 `affc281`, 코드·검증 동결 `3f7fcc1`. GitHub 게시 커밋은 각각 `4693702`, `de14cc3`이며 대응 Git tree가 동일함을 검사했다. 서버 게시 시각과 로컬 연구 시작 시각을 혼동하지 않는다. 전체 실행 manifest에 원본 SHA와 실행 시작 시각을 보존한다.\n\n'
    protocol+='두 가설과 가격 대조군만 평가했으며 경험적 파라미터 탐색0회, 사후 성과 맞춤 규칙 변경0회다. 원본 감사 저장의 cp949 오류 후 UTF-8 옵션으로 동일 코드 재실행한 기록은 [연구 기록](../../REPRICING_V3_WORKLOG.md)에 있다. 기존 브로커와 계좌 엔진은 수정하지 않았다.\n\n'
    protocol+='비용은 편도 기본 수수료10bp+슬리피지10bp, 스트레스20bp+30bp. 현물 무차입이므로 펀딩0. D완료 후 D+2시가보다 빠른 체결은 없다. 진입 이후 달력구간의 결측은 보수적인 전액 상각을 사용한다. 주문장·부분체결·거래소 주문 필터·시가 유동성은 검증되지 않았고, 일봉 존재가 실제 체결을 보장하지 않는다.\n\n'
    protocol+='연구상 우위와 실거래 관련성의 모든 게이트는 JSON에 고정되어 있다. 연구 우위를 통과해도 실거래 승인으로 보지 않는다. 실패 가설은 REJECT, 성과 실패 없이 표본·필수 감사만 부족한 경우 NEEDS_MORE_EVIDENCE다. 이전 구간을 완전히 미관측된 OOS라고 부르지 않는다.\n'
    (out/'PROTOCOL.md').write_text(protocol,encoding='utf-8')
    text='# INDEPENDENT_REVIEW\n\n'+f"**PASS — 소프트웨어·원장 재현. 투자 우위 승인과 별개.** 별도 Python 프로세스에서 일별 라벨 {review['atlas']['rows']:,}행과 {len(review['accounts'])}개 계좌를 확인했다. 동일 작성자의 별도 구현이므로 외부 제3자 투자감사가 아니다.\n\n"
    text+='- 전체 아틀라스: 미래 연속 경로를 먼저 모아 잘라 계산하는 별도 방식으로90/180일2/3배·MAE·도달시간·종료 상태 재계산. 적격 시장·행수·정렬·중복 확인.\n- 모든 일요일: 원시 이력으로 거래대금·상대성과·TR변화·가설 조건을 재계산. 182일 간격 후보와 날짜별 모집단, D+2/D+3 지연, 보유 동일성 확인.\n- 모든 계좌: 현금·보유수량·수수료·원시 종가 평가액·종가 총위험을 일별 재구성. 캠페인 손익·계좌 손익·요약 수치 일치.\n- 모든 연속 계좌: 신규 객체로 전체 재실행하고, 실제2023년 이후 데이터를 삭제한1,461일의 평가액·체결·상각·계획이 동일함을 확인.\n- 매칭 실패 라벨·동일 날짜/구간·재사용 금지, 비중복 앵커, 확률의 분모를 별도 확인.\n\n'
    text+=table(['계좌','일수','신규진입 후 최대위험','종가 최대위험','종가 최대투입','0거래량 체결'],[
        [k,v['days'],pct(v['max_risk_after_entry']),pct(v['max_daily_close_open_risk']),pct(v['max_daily_close_exposure']),v['fills_on_zero_volume_bars']]
        for k,v in review['accounts'].items() if k.endswith('__stress__continuous')])
    text+='\n원본 ZIP 체크섬28,228개와 파생 일봉844,092개를 검증했다. 시가 유동성·완전한 토큰 동일성·시장별 공식 폐지 사유·실거래 체결은 입증하지 못했다. 현재166개 합성·회귀 테스트와 컴파일·CLI 도움말/상태가 로컬에서 통과했다. CI의 실제 상태는 PR의 해당 HEAD 결과를 따로 확인해야 한다. [상세 감사](independent_review.json), [해시](SHA256.json).\n'
    (out/'INDEPENDENT_REVIEW.md').write_text(text,encoding='utf-8')
    (out/'NEXT_DECISION.md').write_text('# NEXT_DECISION\n\n'+intro.split('\n\n',1)[1]+'\n가격·거래량 실험은 완료했다. 같은 역사 구간에서 임계값을 다시 최적화하거나 레버리지를 늘리는 작업은 다음 행동으로 선정하지 않았다. 새 패널은 발표·투표·실행·매출 귀속·공급 변화를 당시 가용시각과 함께 기록하고, 새 가설과 판정은 새로운 결과 조회 전에 등록해야 한다.\n',encoding='utf-8')
    # Public structured summaries, no raw prices or execution records.
    dump(out/'comparison.json',comparison)
    dump(out/'conditional_rates.json',stats['rates'])
    dump(out/'account_analysis.json',analysis)
    dump(out/'decision.json',analysis['decisions'])
    dump(out/'independent_review.json',review)
    dump(out/'protocol.json',read(ROOT/'docs/REPRICING_V3_PROTOCOL.json'))
    dump(out/'freeze.json',read(ROOT/'docs/REPRICING_V3_FREEZE.json'))
    dump(out/'run_manifest.json',manifest)
    # Coverage contains market metadata only; no raw candles.
    dump(out/'data_audit.json',source)
    dump(out/'atlas_summary.json',atlas)
    dump(out/'local_artifact_hashes.json',read(a.run/'artifact_hashes.json'))
    monthly=[]
    for account,d in analysis['accounts'].items():
        for r in d['monthly']:
            monthly.append(dict(account=account,**r))
    csv_write(out/'monthly_krw.csv',monthly)
    public_markets={r['symbol']:r for r in atlas['markets']}
    coverage=source['collection'].get('coverage',[])
    csv_write(out/'atlas_market_summary.csv',[dict(symbol=r['symbol'],source_bars=r['bars'],
        first_observed_ms=r['first_ms'],last_observed_ms=r['last_ms'],
        **{k:v for k,v in public_markets.get(r['symbol'],{'eligible_daily_rows':0}).items() if k!='symbol'}) for r in coverage])
    csv_write(out/'regime_anchors.csv',atlas['regime_anchors'])
    csv_write(out/'signal_episodes_summary.csv',stats['episode_summary'])
    csv_write(out/'matched_differences.csv',stats['matched_differences'])
    with gzip.open(a.run/'MATCHED_CONTROLS.json.gz','rt',encoding='utf-8') as f:
        pairs=json.load(f)
    pair_rows=[]
    for p in pairs:
        case=p['case']
        for c in p['controls'] or [None]:
            pair_rows.append(dict(era=case['era'],day_ms=case['day'],case_symbol=case['symbol'],
                control_symbol=c['symbol'] if c else '',liquidity_bin=case['liquidity_bin'],age_bin=case['age_bin'],
                case_volume_ratio=case['volume_ratio'],control_volume_ratio=c['volume_ratio'] if c else None,
                case_relative28=case['relative28'],control_relative28=c['relative28'] if c else None,
                case_tr_ratio=case['tr_ratio'],control_tr_ratio=c['tr_ratio'] if c else None))
    csv_write(out/'matched_pairs.csv',pair_rows)
    # Git publishes LF text. Normalize reviewed exports before hashing so the
    # published byte hashes verify on Linux as well as the Windows exporter.
    # Private artifact hashes remain exact original bytes, without conversion.
    for path in out.iterdir():
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b'\r\n',b'\n'))
    dump(out/'SHA256.json',{p.name:file_sha(p) for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256.json'})
    print(json.dumps(dict(published_files=len(list(out.iterdir())),decisions=analysis['decisions'])))


if __name__=='__main__':
    main()
