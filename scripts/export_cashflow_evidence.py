#!/usr/bin/env python3
"""Publish only reviewed aggregate research evidence, never prices/fill ledgers."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def load(p): return json.loads(Path(p).read_text())
def write(p, v): Path(p).write_text(json.dumps(v, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
def pct(x): return f'{100*x:.2f}%'
def number(x): return 'N/A' if x is None else f'{x:.2f}'


def export(run, audit, out):
    run, out = Path(run), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    results = load(run/'comparison.json')
    protocol = load(run/'protocol.json')
    base, stress = (load(run/f'{case}_continuous.json') for case in ('base','stress'))
    cash = {case:load(run/f'{case}_cashflow_target.json') for case in ('base','stress')}
    validation = load(run/'independent_validation.json')
    independent = {Path(k).stem:{key:v for key,v in report.items() if key!='campaigns'} for k,report in validation.items()}
    for report in independent.values():
        report['checks'].pop('zero_volume_fill_details',None)
    s = stress['summary']
    folds = [v for k,v in results['stress'].items() if k!='continuous']
    positive = sum(v['total_return']>0 for v in folds)/len(folds)
    expected = {f'{case}_{fold}' for case,case_results in results.items() for fold in case_results}
    continuous_checks = ('cash_reconciles','inventory_reconciles','campaign_pnl_reconciles',
                         'plan_chronology','buy_delay','reported_summary_matches',
                         'fill_market_bars_present','historical_universe_independently_recomputed',
                         'prefix_equity_fills_writeoffs_plans_identical')
    independent_pass = set(independent)==expected and all(
        v['checks'].get('reported_summary_matches',False) for v in independent.values())
    for case in ('base','stress'):
        check = independent.get(f'{case}_continuous',{}).get('checks',{})
        independent_pass = independent_pass and all(check.get(k,False) for k in continuous_checks) \
            and check.get('fills_on_zero_volume_bars')==0
    checks = {
        'stress_cagr_positive': s['cagr']>0,
        'stress_profit_factor_over_1_2': s['profit_factor'] is not None and s['profit_factor']>1.2,
        'stress_campaigns_at_least_100': s['trade_count']>=100,
        'calendar_folds_at_least_5': len(folds)>=5,
        'positive_calendar_fold_fraction_at_least_60pct': positive>=.60,
        'stress_daily_close_drawdown_within_15pct': s['max_drawdown']>=-.15,
        'no_permanent_halt': not s['latched_drawdown_halt'],
        'independent_validation_completed': independent_pass,
    }
    cash_checks = cash['stress']['cashflow_gate_checks']
    empirical_pass = all(v for k,v in checks.items() if k!='independent_validation_completed') and all(cash_checks.values())
    decision = {
        'classification': 'NEEDS MORE EVIDENCE' if empirical_pass or not independent_pass else 'REJECTED',
        'scope': 'This frozen first price-proxy swing strategy and the fixed-capital cashflow test; not rejection of all future strategies or compounding routes.',
        'research_checks': checks, 'minimum_cashflow_checks': cash_checks,
        'failed_checks': [k for k,v in {**checks,**cash_checks}.items() if not v],
        'positive_stress_calendar_folds': sum(v['total_return']>0 for v in folds),
        'calendar_fold_count': len(folds), 'live_authorized': False,
        'next_experiment_branch': cash['stress']['next_experiment_branch'],
        'next_experiment_numeric_gates': cash['stress']['next_experiment_numeric_gates'],
        'limitations': ['Historical fixed-policy walk-forward, not untouched OOS.',
                       'No publication-timed fundamental evidence, no validated leveraged futures.',
                       'Tax, KRW FX and full execution costs remain outside net simulation returns.'],
    }
    data = load(audit)
    data['collection'].pop('coverage',None)
    compact_cash = {}
    for case,c in cash.items():
        compact_cash[case] = {k:v for k,v in c.items() if k not in ('monthly_rows','rolling12')}
        compact_cash[case]['withdrawal_overlays'] = [{k:v for k,v in row.items() if k!='windows'} for row in c['withdrawal_overlays']]
    write(out/'comparison.json', results)
    write(out/'decision.json', decision)
    write(out/'protocol.json', protocol)
    write(out/'data_audit.json', data)
    write(out/'independent_validation.json', independent)
    write(out/'cashflow_target.json', compact_cash)
    b = base['summary']; collection=data['collection']
    lines = [
      '# ONE Trading — Cashflow Alpha v1 검증 결과', '',
      f"**판정: {decision['classification']}**. 첫 가격·거래량 기반 재평가 선점형 스윙 전략에 대한 판정이며, 실제 주문 승인이 아니다.", '',
      '기존 PR #2 기반에 새 진입·보유 규칙을 구현했다. 기준은 실제 성과를 확인하기 전에 별도 커밋에 고정했다. 기존 Ridge 매수 예측은 재사용하지 않았다.', '',
      f"실제 Binance 현물 데이터: {collection['markets']}개 시장, {collection['daily_bars']:,}개 일봉, 원본 ZIP {data['archive']['publisher_zip_checksums_verified']:,}개. 기간 2018-01~2026-09; 평가 2019-01~2026-09. 모든 정규화 일봉을 원본에서 재생성해 대조했다.", '',
      '## 연속 운용 결과', '',
      '| 항목 | 기본 비용 | 악화 비용 |', '|---|---:|---:|',
      f"| 누적수익률 | {pct(b['total_return'])} | {pct(s['total_return'])} |",
      f"| 연복리 수익률 | {pct(b['cagr'])} | {pct(s['cagr'])} |",
      f"| 일별 종가 최대낙폭 | {pct(b['max_drawdown'])} | {pct(s['max_drawdown'])} |",
      f"| 완료 거래(분할·추가매수 통합) | {b['trade_count']} | {s['trade_count']} |",
      f"| 평균 자본 노출 | {pct(b['average_daily_exposure'])} | {pct(s['average_daily_exposure'])} |",
      f"| 승률 | {pct(b['win_rate']) if b['win_rate'] is not None else 'N/A'} | {pct(s['win_rate']) if s['win_rate'] is not None else 'N/A'} |",
      f"| 평균이익 / 평균손실 | {number(b['payoff_ratio'])} | {number(s['payoff_ratio'])} |",
      f"| Profit factor | {number(b['profit_factor'])} | {number(s['profit_factor'])} |",
      f"| 월 수익률 중앙값 | {pct(b['monthly_median_return'])} | {pct(s['monthly_median_return'])} |",
      f"| 수익 월 비율 | {pct(b['positive_month_fraction'])} | {pct(s['positive_month_fraction'])} |",
      f"| 영구 중단 발생 | {b['latched_drawdown_halt']} | {s['latched_drawdown_halt']} |", '',
      '기본 비용은 편도 수수료 0.10%+슬리피지 0.10%, 악화 비용은 0.20%+0.30%. 무차입 현물이므로 펀딩비는 0. 세금·환전·실제 호가 비용을 모두 반영한 생활비 순수익이 아니다. 비용별로 실제 시뮬레이션 경로가 달라진다.', '',
      '## 연도별: 한 계좌를 계속 운용한 결과', '',
      '| 연도 | 기본 수익률 | 악화 수익률 | 기본 MDD | 완료 거래 | 평균 노출 | 손익비 |',
      '|---|---:|---:|---:|---:|---:|---:|']
    for y,v in base['yearly'].items():
        lines.append(f"| {y} | {pct(v['return'])} | {pct(stress['yearly'][y]['return'])} | {pct(v['max_drawdown'])} | {v['trade_count']} | {pct(v['average_exposure'])} | {number(v['payoff_ratio'])} |")
    lines += ['', '2026년은 9월까지. 연도 MDD는 해당 연초 자산을 기준으로 다시 측정한 그해 낙폭이다. 전체 기간 MDD와 구분한다.', '',
      '## 연도별 독립 워크포워드 진단', '',
      '각 연도 자본·중단 상태를 초기화한 별도 계좌다. 아래 성과를 이어 붙이면 실제 연속 운용 성과가 되지 않는다.', '',
      '| 연도 | 기본 수익률 | 악화 수익률 | 악화 MDD | 악화 완료 거래 |', '|---|---:|---:|---:|---:|']
    for y,v in results['base'].items():
        if y=='continuous':continue
        sv=results['stress'][y]
        lines.append(f"| {y} | {pct(v['total_return'])} | {pct(sv['total_return'])} | {pct(sv['max_drawdown'])} | {sv['trade_count']} |")
    lines += ['', '## 판정 근거', '', '미달한 사전 기준:', ''] + [f'- `{k}`' for k in decision['failed_checks']]
    lines += ['', '## 월 현금흐름 검증', '',
      '자본을 2,000만원으로 유지하면서 매월 500만/1,000만원을 인출하려면 월 25%/50%가 필요하다. 이후 자본증식까지 포함한 장기 목표의 필요 수익률과는 다르다.', '']
    for case,c in compact_cash.items():
        m=c['monthly_observations'];label='기본' if case=='base' else '악화'
        lines.append(f"- {label}: {m['full_calendar_months']}개월 중 월 +25% 이상 {m['months_at_least_25pct']}회, +50% 이상 {m['months_at_least_50pct']}회. 최고 월 {pct(m['net_return_max'])}, 최저 월 {pct(m['net_return_min'])}.")
        for w in c['withdrawal_overlays']:
            lines.append(f"- {label} 월 {w['withdrawal_krw_per_month']/10000:,.0f}만원 인출 가정: 과거 12개월 시작구간 {w['historical_window_count']}개 중 12회 인출 후 자본 잔존 {w['fully_funded_with_capital_remaining_count']}개; 소진 시점 중앙값 {w['depletion_month_median']}개월.")
    lines += ['', '인출 분석은 관측 수익률을 일정하게 유지하는 산술 오버레이다. 추가 매도비용과 최소 주문 제약을 무시하며, 미래 성공확률이나 별도 체결 백테스트가 아니다.', '',
      '## 다음 실험', '',
      f"관측 결과에 따른 우선순위: `{decision['next_experiment_branch']}`.", '',
      '다음 후보는 [고정된 두 개 명세](../../CASHFLOW_ALPHA_NEXT_EXPERIMENT.json)로 제한한다. A는 상대강도 회복·SMA50 재돌파를 주간으로 포착하고, B는 상승 추세에서 고점 대비 8~25% 눌림 후 반등을 일간으로 포착한다. B의 최대 보유는 21일이다. 둘 다 아직 실행하지 않았다.', '',
      '최소 200개 완료 거래, 악화 비용 PF>1.3, 양의 평균 거래손익, 3개월 블록 재표집 월평균 수익 하한(95%)>0, 수익 연도 비율≥60%, 낙폭 중단 15%를 요구한다. 레버리지는 검증 전 1배, 이후에도 실제 선물·펀딩·청산 데이터 검증을 별도로 통과해야 한다. 90일 이상 전진 모의운용은 다음 단계의 증거 조건이며 이 작업에서 수행된 것으로 간주하지 않는다.', '',
      '최근 역사적 도전 구간에서 최소 50거래, 동일 시점 무작위 종목선택 대조군 200개의 97.5백분위 초과도 요구한다. 이미 살펴본 역사는 미사용 OOS로 부르지 않는다. 경제성 하한인 악화 비용 월평균 1%조차 2,000만원당 월 20만원이므로 목표 현금인출을 입증하지 못한다.', '',
      '## 재현 및 제한', '',
      '- [실행 절차](../../CASHFLOW_ALPHA.md), `protocol.json`, `comparison.json`, `independent_validation.json` 참조.',
      '- 원본 DB에는 ZIP·체크섬·수집시각이 포함된다. 개별 체결·일별 자산·신호 기록은 재현 묶음에 별도 보존한다.',
      '- 독립 검증은 원시 체결에서 손익을 재계산하고, D+2 체결·당시 거래량 순위·미래 데이터 절단 불변성을 확인한다.',
      '- 이미 알려진 과거 구간이며 미사용 OOS가 아니다. 재표집 구간은 과거 불확실성 진단이지 미래 수익 확률이 아니다.',
      '- 상장 종료 위험은 종료 시장을 포함하고 결측 보유분을 전액 손실 처리한다. 공시 전체와 토큰 정체성 이력은 완전하지 않다.',
      '- 일봉의 동시 저가 가정은 일중 위험 진단일 뿐 실제 가격 경로나 체결 가능한 최대낙폭이 아니다.',
      '- 사업 사용량·매출·언락의 발표시점 데이터가 연결되지 않아 가격 대용신호 이상의 재평가 검증은 아니다.',
      '- main 및 PR #1·#2는 변경·병합하지 않았다. 실주문은 실행하지 않았다.', '']
    (out/'RESULTS.md').write_text('\n'.join(lines))
    write(out/'SHA256.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file()})
    print(json.dumps(decision,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True);p.add_argument('--data-audit',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();export(a.run,a.data_audit,a.output)


if __name__=='__main__':main()
