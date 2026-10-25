"""Export reviewed aggregates; no prices or individual simulation records."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.validate_cashflow_alpha import quantile,drawdown
from scripts.analyze_cashflow_target import monthly_rows

A='relative_strength_recovery';B='established_trend_controlled_pullback'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
def pct(v):return '해당 없음' if v is None else f'{100*v:+.2f}%'
def number(v):return '해당 없음' if v is None else f'{v:.3f}'
def date(t):return datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%d')
def csv_file(p,rows):
    with p.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');w.writeheader();w.writerows(rows)
def table(headers,rows):
    return ['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+['| '+' | '.join(map(str,r))+' |' for r in rows]


def streaks(ps):
    longest=cur=0;running=worst=0.
    for p in sorted(ps,key=lambda p:(p['closed_ms'],p['symbol'],p['opened_ms'])):
        if p['pnl']<0:cur+=1;running+=p['pnl'];longest=max(longest,cur);worst=min(worst,running)
        else:cur=0;running=0.
    return dict(max_consecutive_losing_campaigns=longest,worst_losing_streak_pnl_krw=worst*2000)


def export(run_dir,audit_dir,out):
    out.mkdir(parents=True,exist_ok=True)
    comparison=read(run_dir/'comparison.json');audits=read(audit_dir/'account_audits.json')
    decisions=read(audit_dir/'decision.json');data=read(run_dir/'data_audit/data_audit.json')
    if any(d['decision']!='REJECT' for d in decisions['strategies'].values()):
        raise ValueError('this_frozen_report_requires_both_recorded_rejections')
    write(out/'data_audit.json',{**data,'collection':{k:v for k,v in data['collection'].items() if k!='coverage'}})
    csv_file(out/'market_coverage.csv',[dict(symbol=x['symbol'],bars=x['bars'],first_observed_bucket=date(x['first_ms']),
        last_observed_bucket=date(x['last_ms']),missing_days=x['missing_days'],evidence='daily buckets, not official listing notices') for x in data['collection']['coverage']])
    write(out/'comparison.json',{s:{c:{f:{k:v for k,v in summary.items() if k!='execution_notes'} for f,summary in folds.items()}
                               for c,folds in costs.items()} for s,costs in comparison.items()})
    for name,v in [('decision.json',decisions),('independent_validation.json',audits),
                   ('run_manifest.json',read(run_dir/'run_manifest.json')),('private_artifact_hashes.json',read(run_dir/'artifact_hashes.json'))]:write(out/name,v)
    write(out/'validation_artifact_hashes.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(audit_dir.glob('*.json'))})
    monthly=[];yearly=[];folds=[];eras=[];metrics={};controls_public={}
    for s in (A,B):
        controls=read(run_dir/f'{s}__controls.json');vals=[r['mean_monthly_net_return'] for r in controls]
        controls_public[s]=dict(count=len(controls),statistic='stress mean full-month net return',
            quantiles={str(p):quantile(vals,p) for p in (0,.025,.25,.5,.75,.975,1)},
            runs=[dict(seed=x['seed'],mean_monthly_net_return=x['mean_monthly_net_return'],
                total_return=x['summary']['total_return'],profit_factor=x['summary']['profit_factor'],
                trade_count=x['summary']['trade_count'],max_drawdown=x['summary']['max_drawdown'],
                ledger_sha256=x['ledger_sha256'],ledger_checks=x['ledger_checks']) for x in controls])
        for case in ('base','stress'):
            for name,summary in comparison[s][case].items():
                r=read(run_dir/f'{s}__{case}__{name}.json')
                folds.append(dict(strategy=s,cost=case,account=name,
                    **{k:summary[k] for k in ('total_return','cagr','max_drawdown','profit_factor','win_rate','trade_count','average_daily_exposure','latched_drawdown_halt')},
                    average_holding_days=statistics.mean(p['holding_days'] for p in r['campaigns']) if r['campaigns'] else None))
                if name!='continuous':continue
                key=f'{s}__{case}__continuous';ind=read(audit_dir/f'{key}_campaign_audit.json');ps=ind['campaigns']
                full=[x for x in monthly_rows(r) if x['full_calendar_month']];returns=[x['equity_net_return'] for x in full]
                m={**ind['metrics'],**streaks(ps),
                    'average_holding_days':statistics.mean((p['closed_ms']-p['opened_ms'])/86_400_000 for p in ps) if ps else None,
                    'idle_capital_fraction':1-summary['average_daily_exposure'],'mean_full_month_net_return':statistics.mean(returns),
                    'monthly_return_quantiles':{str(p):quantile(returns,p) for p in (0,.05,.25,.5,.75,.95,1)},
                    'monthly_positive':sum(v>0 for v in returns),'monthly_zero':sum(v==0 for v in returns),
                    'monthly_negative':sum(v<0 for v in returns),'full_month_count':len(full),
                    'fixed20m_hit5m_months':sum(v>=.25 for v in returns),'fixed20m_hit10m_months':sum(v>=.50 for v in returns),
                    'compounding20m_hit5m_months':sum((x['end_equity']-x['begin_equity'])*2000>=5_000_000 for x in full),
                    'compounding20m_hit10m_months':sum((x['end_equity']-x['begin_equity'])*2000>=10_000_000 for x in full),
                    'fees_paid_krw':summary['fees_paid']*2000,'slippage_cost_krw':summary['slippage_cost']*2000,
                    'bootstrap':audits[key]['bootstrap'],'risk_checks':audits[key]['checks'],
                    'intraday_simultaneous_low_bound':summary['intraday_low_drawdown_bound']}
                metrics[f'{s}__{case}']=m
                for x in full:
                    monthly.append(dict(strategy=s,cost=case,month=x['month'],net_return=x['equity_net_return'],
                        fixed_20m_pnl_krw=x['equity_net_return']*20_000_000,
                        original_account_pnl_krw=(x['end_equity']-x['begin_equity'])*2000,
                        realized_pnl_krw=x['realized_pnl_including_writeoffs']*2000,
                        account_begin_equity_krw=x['begin_equity']*2000,account_end_equity_krw=x['end_equity']*2000,
                        hit_5m_on_fixed20m=x['equity_net_return']>=.25,hit_10m_on_fixed20m=x['equity_net_return']>=.50))
                for year,v in r['yearly'].items():
                    p=[t for t in ps if date(t['closed_ms']).startswith(year)]
                    win=[t['pnl'] for t in p if t['pnl']>0];loss=[-t['pnl'] for t in p if t['pnl']<0]
                    yearly.append(dict(strategy=s,cost=case,year=year,net_return=v['return'],max_drawdown=v['max_drawdown'],
                        profit_factor=sum(win)/sum(loss) if loss else None,win_rate=len(win)/len(p) if p else None,
                        closed_campaigns=len(p),average_holding_days=statistics.mean((t['closed_ms']-t['opened_ms'])/86_400_000 for t in p) if p else None,
                        average_exposure=v['average_exposure'],original_account_pnl_krw=(v['final_equity']-v['initial_equity'])*2000))
                for era,(first,last) in read(ROOT/'docs/CASHFLOW_ALPHA_V2_PROTOCOL.json')['era_accounts'].items():
                    i=next(i for i,x in enumerate(r['equity']) if date(x['ts_ms'])>=first)
                    rows=[x for x in r['equity'][i:] if date(x['ts_ms'])<last]
                    initial=r['equity'][i-1]['equity'] if i else r['config']['initial_cash']
                    eras.append(dict(strategy=s,cost=case,era=era,net_return=rows[-1]['equity']/initial-1,
                        max_drawdown=drawdown([x['equity'] for x in rows],initial),
                        average_exposure=statistics.mean(x.get('exposure_before_end_liquidation',x['exposure']) for x in rows)))
    write(out/'metrics.json',metrics);write(out/'random_controls.json',controls_public)
    csv_file(out/'monthly_krw.csv',monthly);csv_file(out/'yearly_continuous.csv',yearly)
    csv_file(out/'independent_accounts.csv',folds);csv_file(out/'era_continuous_attribution.csv',eras)
    ordered=[metrics[f'{s}__{c}'] for s in (A,B) for c in ('base','stress')]
    headers=['지표','A 기본','A 스트레스','B 기본','B 스트레스']
    lines=['# Cashflow Alpha v2 — 두 후보 검증 결과','',
        '**A: REJECT / B: REJECT. 채택 전략 없음.** 실제 주문·실계좌·레버리지는 사용하지 않았다.','',
        '## 전체 성과','',
        '2019-01-01~2026-09-30 UTC 연속 계좌. 수수료·슬리피지 차감 후 USDT 수익률이며 세금·원화 환율 변화는 제외한다. A는 상대강도 회복, B는 확립된 추세의 통제된 눌림이다.','']
    rows=[]
    for label,k,fmt in [('누적수익률','total_return',pct),('CAGR','cagr',pct),('최대 낙폭(일별 종가)','daily_close_mdd',pct),
        ('Profit Factor','profit_factor',number),('승률','win_rate',pct),('종료 거래 수','closed_campaign_count',str),
        ('평균 보유일','average_holding_days',number),('평균 자본 투입률','average_daily_exposure',pct),
        ('유휴자본 비율','idle_capital_fraction',pct),('평균 월수익률','mean_full_month_net_return',pct),
        ('최대 연속 손실 거래','max_consecutive_losing_campaigns',str)]:rows.append([label]+[fmt(x[k]) for x in ordered])
    lines+=table(headers,rows)+['','부분익절과 축소 매도는 별도 거래로 부풀리지 않고 최초 진입부터 전량 청산까지 한 거래로 묶었다. 연속 손실은 종료 시각·종목명·진입 시각 순서로 집계하며 비손실 거래가 연속을 끊는다.','',
        '## 월간 분포와 2,000만 원 환산','']
    rows=[]
    for label,k in [('최저 월','0'),('5% 분위','0.05'),('25% 분위','0.25'),('중앙값','0.5'),('75% 분위','0.75'),('95% 분위','0.95'),('최고 월','1')]:rows.append([label]+[pct(x['monthly_return_quantiles'][k]) for x in ordered])
    for label,k in [('수익 월','monthly_positive'),('보합 월','monthly_zero'),('손실 월','monthly_negative'),('월 500만 원 달성','fixed20m_hit5m_months'),('월 1,000만 원 달성','fixed20m_hit10m_months')]:rows.append([label]+[f"{x[k]}/{x['full_month_count']}" for x in ordered])
    lines+=table(headers,rows)+['',
        '[monthly_krw.csv](monthly_krw.csv)에 모든 93개월 × 4조건의 수익률과 원화 손익을 실었다. `fixed_20m_pnl_krw`는 매월 고정 자본 2,000만 원 × 월 순수익률, `original_account_pnl_krw`는 초기 2,000만 원 계좌의 복리 평가손익, `realized_pnl_krw`는 해당 월 매도·손실상각으로 확정된 손익이다. 실제 인출 가능 현금흐름으로 동일시하지 않는다. 복리 계좌의 월손익 기준 두 목표 달성 횟수도 metrics.json에 기록했다.','']
    rows=[]
    for label,vals in [('평균 고정자본 월손익',[x['mean_full_month_net_return']*20_000_000 for x in ordered]),('최저 고정자본 월손익',[x['monthly_return_quantiles']['0']*20_000_000 for x in ordered]),
        ('최고 고정자본 월손익',[x['monthly_return_quantiles']['1']*20_000_000 for x in ordered]),('연속손실 최악 누적손익',[x['worst_losing_streak_pnl_krw'] for x in ordered]),
        ('전체 수수료',[x['fees_paid_krw'] for x in ordered]),('전체 슬리피지 비용',[x['slippage_cost_krw'] for x in ordered])]:rows.append([label]+[f'{v:,.0f}원' for v in vals])
    lines+=table(headers,rows)+['','월 500만/1,000만 원은 현 자본으로 각각 월 +25%/+50%가 필요하다. 이는 목표의 자본 산술이며 가능한 기대수익률이라는 가정이 아니다. 실제 세후 원화 손익에는 환율·세금·환전 비용을 별도로 반영해야 한다.','',
        '## 연도별 연속 계좌 성과','',
        '연초에 자본·누적 손실을 초기화하지 않았다. PF·승률·거래 수는 해당 연도 종료 거래 기준이며, 이익 거래만 있어 PF 분모가 0이거나 거래가 없으면 해당 없음으로 표시한다. 2026년은 1~9월이다.','']
    lines+=table(['전략','비용','연도','수익률','낙폭','PF','승률','거래','보유일','자본 투입률'],
        [['A' if x['strategy']==A else 'B',x['cost'],x['year'],pct(x['net_return']),pct(x['max_drawdown']),number(x['profit_factor']),pct(x['win_rate']),x['closed_campaigns'],number(x['average_holding_days']),pct(x['average_exposure'])] for x in yearly])
    lines+=['','## 시기별 독립 계좌','',
        '개발 2019~2021, 검증 2022~2024, 도전 2025~2026년 9월. 각 시기의 자본·위험 상태를 초기화한 진단 계좌이며 연속 계좌 대용으로 이어 붙이지 않았다.','']
    lines+=table(['전략','비용','시기','수익률','CAGR','낙폭','PF','승률','거래','보유일','투입률'],
        [['A' if x['strategy']==A else 'B',x['cost'],x['account'],pct(x['total_return']),pct(x['cagr']),pct(x['max_drawdown']),number(x['profit_factor']),pct(x['win_rate']),x['trade_count'],number(x['average_holding_days']),pct(x['average_daily_exposure'])] for x in folds if x['account'] in {'development','validation','challenge'}])
    lines+=['','[independent_accounts.csv](independent_accounts.csv)에 연속 4개·연도별 독립 32개·시기별 독립 12개, 총 48개 계좌를 기록했다. 연속 계좌의 시기별 귀속 수익은 [era_continuous_attribution.csv](era_continuous_attribution.csv)에 제공한다.','',
        '## 사전등록 판정','',
        '스트레스 비용 기준이다. 표본 부족만 남으면 NEEDS_MORE_EVIDENCE이지만, 측정된 기대값·PF·경제성 기준 실패가 있으면 REJECT다. 원래 두 후보 설계 JSON과 v1 기각 보고서는 보존했다.','']
    labels=dict(stress_pf='PF > 1.30',total_sample='전체 거래 ≥ 200',challenge_sample='도전 계좌 거래 ≥ 50',expectancy='평균 거래 손익 > 0',positive_calendar_folds='흑자 독립 연도 ≥ 60%',
        max_drawdown='연속·독립 계좌 일별 MDD ≤ 15%',no_permanent_halt='영구 중단 없음',bootstrap_positive_lower='월 평균 순수익 95% 구간 하단 > 0',
        random_control='대조군 백분위 ≥ 97.5%',monthly_growth='월 평균 순수익 ≥ 1%',positive_months='수익 월 ≥ 55%',required_checks='필수 시계열·회계 검증 완료')
    lines+=table(['기준','A','B'],[[label]+['충족' if decisions['strategies'][s]['gates'][k] else '미달' for s in (A,B)] for k,label in labels.items()])
    for s in (A,B):
        d=decisions['strategies'][s];ci=metrics[f'{s}__stress']['bootstrap']['monthly_mean_95pct_interval']
        lines+=['',f"- {'A' if s==A else 'B'}: **{d['decision']}**. 대조군 백분위 {100*d['control_percentile']:.1f}%, 단측 Monte Carlo 비교 p={d['control_one_sided_monte_carlo_p']:.4f}. 월 평균 순수익의 과거 블록 재표본 95% 구간 [{pct(ci[0])}, {pct(ci[1])}]."]
    lines+=['','대조군은 후보마다 스트레스 계좌 200개, seed 0~199다. 원래 신호일의 후보 개수만큼 당일 과거 적격 풀에서 추출하고 같은 비용·체결·청산·위험 한도를 사용했다. 재표본 구간과 대조군 순위는 과거 비교 통계이며 미래 성공확률이 아니다.','',
        '## 총위험','']
    rows=[['일중 동시 저가 낙폭 하한']+[pct(x['intraday_simultaneous_low_bound']) for x in ordered],
          ['신규 진입 직후 최대 총위험']+[pct(x['risk_checks']['max_risk_after_entry']) for x in ordered],
          ['종가 기준 최대 총위험']+[pct(x['risk_checks']['max_daily_close_open_risk']) for x in ordered]]
    lines+=table(headers,rows)+['','신규 진입 직후 총위험 3%는 지켰지만 이후 가격 변동과 스톱 조정 지연으로 종가 총위험이 이를 넘었다. 상시 3% 총위험 유지에 성공했다고 판단하지 않는다. 동시 저가 하한은 보호 스톱을 무시하는 보수적 진단이며 실제 체결 MDD가 아니다. 3% 일일 손실과 15% 낙폭은 중단 트리거로, 실제 장중·갭 손실을 보장하지 않는다.','',
        '## 해석과 남은 불확실성','',
        'A는 기회가 희박하고 스트레스 거래 기대값도 음수다. B는 표본과 기본 비용의 양수 성과를 보였지만 스트레스 PF 및 월 현금흐름 경제성이 부족하다. 필터 탈락 이유는 서로 겹치므로 개별 필터의 인과 효과로 해석하지 않는다. 결과에 맞춘 파라미터·빈도·레버리지 변경은 하지 않았다.','',
        '가격 규칙 세 가지(v1과 이번 두 후보)의 실패는 해당 규칙과 비용·지연 가정에 대한 증거다. 모든 가격 기반 전략의 불가능성을 증명하지 않는다. 가격 회복 자체는 사용량 증가·공급 축소·토큰 보유자 가치귀속을 입증하지 못한다.','',
        '현재 거래소 목록 대신 과거 전체 ZIP의 가용성과 당일 유동성을 사용하고 종료 이력 253개를 유지했다. 240일 연속 이력·갭·LUNA 식별 경계·누락 보유 전액상각을 적용했지만, 전 시장의 공식 상장·폐지 공지와 토큰 동일성 이력을 인증한 자료는 아니다. 생존자 편향을 완전히 제거했다고 주장하지 않는다.','',
        '유휴자본은 수익률에 포함되며 이자를 가정하지 않았다. 펀딩 0은 무차입 현물에만 해당한다. 일봉은 실제 주문장 깊이·체결 순서·부분체결·거래소 필터·실제 지연을 재현하지 못한다. 과거 구간은 v1에서 이미 관찰했으므로 미관측 OOS가 아니다. 동일 작성자의 별도 검증 프로그램이며 외부 제3자의 투자감사는 아니다.','',
        '독립 검증 범위는 [INDEPENDENT_REVIEW.md](INDEPENDENT_REVIEW.md), 다음 연구 비교는 [NEXT_RESEARCH.md](NEXT_RESEARCH.md), 재현 절차는 [CASHFLOW_ALPHA_V2.md](../../CASHFLOW_ALPHA_V2.md)를 참조한다.']
    (out/'RESULTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
    write(out/'SHA256.json',{p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in sorted(out.iterdir()) if p.is_file() and p.name!='SHA256.json'})
    print(json.dumps(dict(exported=out.as_posix(),accounts=len(folds),months=len(monthly))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--audit',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();export(a.run,a.audit,a.output)
