# ONE Zero Discovery v0.1 — 실행 가능한 첫 연구 엔진

## 본질

**매매법을 미리 정하지 않고, 시장의 완성된 과거 관측만 보면서 행동 프로그램을 생성·변형·경쟁시키는 실험장.**

이것은 AlphaGo Zero 자체나 거대언어모델도, 실전 수익이 입증된 AI도 아니다. **유전적 프로그래밍(Genetic Programming)** 방식의 CPU 경량 구현으로, 이후 LLM의 가설 생성·세계모델·강화학습을 연결할 수 있는 첫 실행형 골격이다. 생성 AI 모델 호출·거래소 호출·실제 주문·스케줄러·네트워크 요청은 없다.

## 무엇이 돌아가는가

1. `timestamp,symbol,open,high,low,close,volume` CSV를 검증한다. 모든 종목·봉 시간이 동일해야 하고, 결측·역순·중복·비정상 OHLC를 거부한다. `funding_rate`, `oi`, `aggressive_buy`, `aggressive_sell`, `bid_depth`, `ask_depth`, `spread_bps`를 선택적으로 읽는다. **선물 펀딩비 열이 없다면 0으로 가정되며, 이는 실제 선물 성과가 아니다.**
2. 사람이 이름 붙인 매수 신호 대신 완성봉의 기초 관측값을 만들어낸다. 가격 변화·거래량 변화·시장 내 상대 차이·선물 흐름, 현재 보유/낙폭 등. 현재 버전의 관측 표현은 아직 설계자가 제한한다.
3. 안전한 수식 트리(`add,sub,mul,min,max,tanh,abs,neg`)로 매수(롱)·매도(숏)·현금화·유지 점수 네 개와 크기 점수 하나를 만든다. 무작위 생성·변이·교배 후 경쟁한다. `eval/exec` 또는 외부 작성 코드를 실행하지 않는다.
4. 봉이 **완전히 닫힌 후**에만 판단하고, **다음 봉 시가**에 가상 체결한다. 진입·청산 각 측 비용, 스프레드(제공 시), 펀딩(제공 시), 총/종목 노출, 고점 대비 손실 가드를 반영한다.
5. 시간순 60% train / 20% 내부 validation / 20% 격리 holdout. 탐색은 train·validation만 보고, 마지막 우승 정책을 동결한 다음에만 holdout을 연다. 현금 및 기초 매수·보유 대조군도 별도 계좌로 비교한다.
6. 종목 원자료의 SHA256, 우승 정책, 실험 설정, 세대별 점수, 체결·판단 사건, 계좌 경로를 보존한다. 같은 원본을 이용한 결정론적 재실행과 **별도 회계 재계산**을 지원한다. 단, 전문적인 독립 감사의 완전한 대체는 아니다.
7. 유효한 승자가 없으면 **현금(CASH)** 을 선택한다. 추가 연구 승격 조건으로 현금뿐 아니라 단순 매수·보유 대조군보다 높은 외부 시험 결과도 요구한다. 테스트용 합성 시장에서 수익을 억지로 만들어내지 않는다.

## 설치

Python 3.10 이상, 표준 라이브러리만 사용한다. GitHub `NEONE86/coinkuma` 저장소에서는 리포지토리 루트에서 다음 명령을 실행한다. 이 ZIP 단독으로도 동일하게 실행된다.

```bash
python -m unittest discover -s tests -p 'test_discovery_zero.py' -v
python -m one_trading.discovery_zero demo --output /tmp/one_zero_synthetic.csv --bars 900 --world noedge
python -m one_trading.discovery_zero demo --output /tmp/one_zero_hidden_signal.csv --bars 600 --world leadflow
python -m one_trading.discovery_zero run --data /tmp/one_zero_synthetic.csv --source-kind SYNTHETIC_DEMO --output /tmp/one_zero_synthetic_report.json --population 12 --generations 3
python -m one_trading.discovery_zero verify --data /tmp/one_zero_synthetic.csv --report /tmp/one_zero_synthetic_report.json
```

Windows PowerShell에서는 `/tmp/...` 대신 폴더와 경로를 정하고, 최초 출력 대상은 존재하지 않아야 한다. 같은 파일 덮어쓰기를 금지한다.

실제 CSV 데이터(오프라인)가 준비된 경우:

```bash
python -m one_trading.discovery_zero run --data C:\\private\\market_4h.csv --source-kind EXTERNAL_HISTORICAL_UNVERIFIED --output C:\\private\\one_zero_run_001.json --population 24 --generations 6 --seed 77
python -m one_trading.discovery_zero verify --data C:\\private\\market_4h.csv --report C:\\private\\one_zero_run_001.json
```

CSV에 제공한 데이터의 출처는 자동 검증되지 않는다. `source-kind`는 **사용자가 붙이는 설명**이며 인증 증빙이 아니다.

## CSV 계약

UTC 시간은 반드시 시간대를 포함해야 한다. 예를 들어:

```csv
timestamp,symbol,open,high,low,close,volume,funding_rate
2025-01-01T00:00:00+00:00,BTCUSDT,94000,94500,93500,94300,50000000,0.0001
2025-01-01T04:00:00+00:00,BTCUSDT,94300,94800,94100,94700,51000000,0
```

위 두 줄은 형식 설명일 뿐, 실제 시장 원자료가 아니다. 각 종목별 최소 400봉, 모든 종목 동일 시각 집합이 필요하며, 시간 간격은 일정해야 한다. 전체 데이터의 최소 200봉 검사를 거친 후 훈련·검증·시험 3구간을 분리한다. 별도의 수집·전처리 코드에서 미완성 봉, 서버 수신 지연, 미래 데이터 후행 공개를 처리해야 한다. 과거 4시간봉 기준의 다음 시가 체결은 실시간 관측과 동일하지 않다.

## 검증 대상과 승격 기준

- **코드 재현성 PASS:** 합성 자료에서 시간 인과성, 다음 봉 시가, 비용·원금 대사, 역방향 포지션, 타임스탬프 불일치, 변조 거부를 테스트한다.
- **알파 검증:** 미실시. 합성 자료의 계좌 수익은 금융 우위가 아니다.
- **실제 시장 대조:** 원본 수집 규격과 역사적 상장/폐지 전체 후보군을 갖춘 뒤 수행한다. 수익이 큰 종목을 나중에 골라 투입하는 생존편향을 차단해야 한다.
- **최종 승격:** 미래의 거래소 실시간 수신 데이터에서, 정책을 고정한 대조 실험이 비용 차감 후 현금·매수보유·기존 전략을 통계적으로 능가해야 한다. 이 구현의 `RESEARCH_CHALLENGER_POSITIVE`는 결코 실주문 승격이 아니다.

## 개선 우선순위

1. 기존 `one_trading.ai_native`의 원본 수신시각·실행 지연·실거래 가능 호가·시장 깊이/유동성 제약을 동일 비용 모델로 재사용한다. 이번 v0.1의 4시간봉 가상 시가는 현재 가용한 간소화 가정일 뿐이다.
2. 현재 AST가 표현하지 못하는 **기억·상태·장기 의사결정**을 위해 시퀀스 정책(Transformer/상태 공간 모델)과 세계모델을 추가한다. 이는 아직 구현되지 않았다.
3. 언어모델이 안전한 AST 후보·가설을 제안하는 연결을 승인·비용 예산 내에서 별도로 구현한다. 지금 `--proposals`는 이미 검증 가능한 JSON 후보를 받기만 하며 모델을 호출하지 않는다.
4. 서로 다른 수익 국면을 실제로 복원해, 가짜 신호·의도적인 평가기 속임수·시뮬레이터 내부 허점을 공격하는 반례를 만든다.
5. 세대별 새로운 아이디어 증가 대비 **실제 경제적 성과 개선량**을 기록하고 개선이 없으면 자동 중단한다. 연구는 수익 증식의 수단이지 목적이 아니다.

## 한계와 현재 상태

- 4시간봉 OHLCV만으로는 진짜 호가 안쪽 체결 순서·슬리피지·시장충격·강제청산을 계산할 수 없다. 순수 선물 전략 검증에 바로 쓰면 안 된다.
- 손절/청산을 사람의 55봉·ATR 규칙으로 고정하지 않았지만, 기초 관측값·액션 스키마·연산 수식 언어는 인간이 설계했다. 처음부터 범용 초지능이라고 주장하지 않는다.
- 희귀한 큰 승자 몇 개에 수익이 몰리는 문제, 반복 평가로 사용된 holdout, 시장 레짐 변화, 거래소 상장폐지 편향을 해결한 것이 아니다.
- 임의의 외부 코드, 거래 키, 실제 주문, 지불 가능한 모델 호출은 허용하지 않는다. `--proposals`는 안전한 AST 데이터로 제한된다.
- 명목 노출 최대 2배 기능은 실거래 위험 엔진이 아니라 연구용 단순 노출 제약이며, 실제 선물의 유지증거금·ADL·청산을 모델링하지 않는다.

## 완성된 첫 기능 검증 (2026-11-06)

- `--world noedge` 900봉 3종목 합성 시장: 총 **146개 고유 후보** 평가. 수익 우위가 부족해 **CASH** 선택, 검증 통과.
- `--world leadflow` 600봉 3종목 합성 시장: 가격 변화를 일으키는 *선행 체결 불균형*이 있는 세계를 먼저 만들되 그 진입 규칙을 AI에는 입력하지 않았다. **70개 고유 후보** 평가 후 내부 검증과 분리 시험에서 우수 정책을 선택. 마지막 120봉의 가상 순자본 **1.930배**, CASH 1.000배, 매수·보유 0.936배. 검증 통과.
- 이 두 숫자는 **전적으로 통제된 합성세계의 소프트웨어 검증**이다. 금융시장 거래에 적용할 실제 수익성 증거는 **0건**이다.
- `unittest` 19개 회귀·인과·변조 테스트. 합성 테스트 수만을 통과했으며 독립적인 실거래 검증은 시작하지 않았다.
