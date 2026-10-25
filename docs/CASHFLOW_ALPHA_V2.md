# Cashflow Alpha v2 재현 절차

두 후보 모두 **REJECT**, 채택 전략은 없다. [한국어 결과](experiments/cashflow-v2-2026-10-25/RESULTS.md), [독립 계산 검토](experiments/cashflow-v2-2026-10-25/INDEPENDENT_REVIEW.md), [다음 연구 비교](experiments/cashflow-v2-2026-10-25/NEXT_RESEARCH.md)를 함께 읽는다.

## 계보와 고정 기록

- PR #3 HEAD `e6a78766aeb4462fc4aeb01f80fcbf24ca7096cb`에서 `research/cashflow-alpha-v2-20261025`를 생성했다. main에서 개발하지 않았다. 기존 PR 병합과 기존 결과 수정은 없다.
- 원본 [두 후보 설계](CASHFLOW_ALPHA_NEXT_EXPERIMENT.json)와 [v1 검토](CASHFLOW_ALPHA_INDEPENDENT_REVIEW.md)를 변경하지 않았다. 실행 세부 해석은 [v2 사전등록](CASHFLOW_ALPHA_V2_PROTOCOL.json)에 성과 계산 전에 고정했다.
- 첫 로컬 고정 커밋 `81ba51e`는 Windows CRLF와 Git LF 차이로 실행 전 검사에서 중단됐다. 전략 성과는 생성되지 않았다. 첫 파일 `freeze.json`도 보존했다.
- 유효 로컬 고정 커밋은 `2b9a6a58c2a989ef6be8ba39132d7045badcb707`, 파일은 [freeze-portable.json](experiments/cashflow-v2-2026-10-25/freeze-portable.json)이다. Python 원본·테스트·설정 등 37개 파일의 LF 정규화 SHA-256이 고정돼 있다. 가격과 실행 산출물의 바이너리 해시는 정규화하지 않는다.
- Git 인증 대기를 중단하고 연결된 GitHub 앱으로 게시했다. GitHub 코드 커밋 `15722646c1d0dfcb697ffc93a094dcaa633b5073`의 트리 SHA는 로컬 고정 커밋과 같은 `317e1daf8ac77b0c33feeca74fc69e8cb19b07e3`이다. 작성·게시 경로가 달라 커밋 SHA는 다르며, GitHub 게시 시각을 성과 전 고정 시각이라고 주장하지 않는다. 사전등록·실행 manifest가 로컬 고정 시각과 코드를 기록한다.
- 결과 확인 후 전략·파라미터·비용·기간·대조군·판정 코드를 바꾸지 않았다. `tools/export_cashflow_v2.py`는 고정된 실행·검증 결과를 표와 CSV로 옮기는 후처리다.

## 데이터와 실행 가정

Binance 공식 현물 USDT 월간 일봉 ZIP 전체를 2018-01-01~2026-09-30에 대해 다시 수집했다. 현재 거래 종목만 선택하지 않는다. ZIP 28,228개, 시장 756개, 정규화 일봉 844,092개다. 원시 소스 목록과 일봉 해시는 PR #2·#3과 일치한다. DB 파일 자체의 해시는 수집 시각·SQLite 기록 때문에 다르다.

| 증거 | SHA-256 |
|---|---|
| URL·원시 바이트 해시 목록 | `810d1b1265dbb9531df2b6b89718c1a106efafcdde858eb6a6924b078c0228bf` |
| 정규화 일봉 | `b139383a4eacb5cf9150354576eb9cf2f44937c9a3e019bacfa88f3e37d2b98f` |
| 이번 SQLite 파일 | `84e6c12f0a62bb689d490e861a20ff0c867b6448729c04a2a1cb512330f1fba6` |

각 원시 URL·수집 시각·원시 바이트 SHA-256은 DB의 `sources`와 `snapshots`에 있다. 저장된 ZIP을 다시 해석해 모든 파생 일봉을 대조했으며 모든 공식 CHECKSUM도 확인했다. 공개 파일에는 가격과 개별 거래 원장을 넣지 않고 집계 지표·시장 가용 이력·아티팩트 해시를 담았다. 상위 공식 파일이 수정되면 재수집 결과가 달라질 수 있으므로 정확한 재현에는 저장한 DB·HTTP 캐시가 필요하다. 공식 파일 형식과 체크섬 규약은 [Binance 원문](https://github.com/binance/binance-public-data)에 따른다.

각 신호는 D 완료 종가에서 계산하고 D+1에 관찰하며 가장 빠른 체결은 D+2 시가다. A는 일요일 완료 후 주간, B는 일간 계획이다. 기존 스톱은 매일 작동하고 같은 일봉에서 스톱·익절 모두 닿으면 스톱을 우선한다. 현물 무차입·무레버리지, 펀딩 0, 유휴 현금 이자 0이다. 기본 편도 수수료/슬리피지는 10/10bp, 스트레스는 20/30bp이며 비용별로 경로를 새로 실행한다.

## 다시 실행

Windows Python 3.13에서 실행했으며 표준 라이브러리만 사용한다. Windows와 Linux에서 `-X utf8`를 권장한다. 아래 경로들은 새 출력 폴더여야 한다. 이전 산출물을 덮어쓰지 않는다.

```sh
python -X utf8 -m unittest discover -s tests -v
python -X utf8 -m compileall -q one.py one_trading tests scripts tools
python -X utf8 one.py --help
python -X utf8 one.py status --workspace workspace_cashflow_v2_check

python -X utf8 scripts/collect_cashflow_v2.py --db archive/cashflow-v2-reproduction/market.sqlite3 --cache archive/cashflow-v2-reproduction/http --workers 32

python -X utf8 scripts/run_cashflow_v2.py --freeze docs/experiments/cashflow-v2-2026-10-25/freeze-portable.json --archive archive/cashflow-v2-reproduction/market.sqlite3 --output output/cashflow-v2-reproduction

python -X utf8 scripts/validate_cashflow_v2.py --run output/cashflow-v2-reproduction --archive archive/cashflow-v2-reproduction/market.sqlite3 --output output/cashflow-v2-reproduction-audit

python -X utf8 tools/export_cashflow_v2.py --run output/cashflow-v2-reproduction --audit output/cashflow-v2-reproduction-audit --output output/cashflow-v2-reproduction-public
```

실행기는 커밋된 고정 파일과 코드 해시를 먼저 검사한다. 바뀐 코드로 기존 고정 실험을 실행하면 거부한다. 새로운 가설·파라미터·체결 모델은 별도 실험 버전과 새 사전등록이 필요하다. 단순 재현에는 `--freeze-only`로 기존 고정 파일을 다시 만들지 않는다.

## 읽을 파일

- `RESULTS.md`: 필수 성과, 연도·시기별 표, 비용별 판정.
- `monthly_krw.csv`: 93개월 × 4조건, 고정 2,000만 원 월손익·복리 계좌 평가손익·실현손익.
- `yearly_continuous.csv`: 자본을 초기화하지 않은 연도별 귀속.
- `independent_accounts.csv`: 48개 계좌와 보유기간·자본 투입률.
- `market_coverage.csv`: 과거 756개 시장의 관측 첫날·마지막 날·갭. 공식 상장·폐지 날짜와 구분한다.
- `decision.json`, `random_controls.json`, `independent_validation.json`: 모든 고정 문턱, 400개 대조군, 별도 계산 결과.
- `v1_engine_parity.json`: 공유 엔진 확장 뒤 기존 v1의 실데이터 기본·스트레스 주요 지표가 동일한지 재실행한 회귀 증거. `tools/check_cashflow_v1_parity.py`로 다시 확인할 수 있다.
- `private_artifact_hashes.json`: 비공개 원시 실행 파일들의 해시. 공개에 포함하지 않은 파일을 포함하므로 저장된 원장을 대상으로 확인한다.
- `validation_artifact_hashes.json`: 별도 검증 프로세스의 원본 산출물 해시.
- `SHA256.json`: 공개 산출물의 CRLF→LF 정규화 해시. BOM은 제거하지 않는다. Git의 텍스트 줄바꿈과 독립적으로 확인할 수 있다. `run_manifest.json`의 고정 파일 해시는 실행 당시 Windows 원본 바이트 기준이어서 이 공개 텍스트 정규화 해시와 구분한다. 가격 DB·원시 응답·비공개 실행 산출물의 해시는 모두 원본 바이트 기준이다.

별도의 선물 데이터·펀딩·마크가격·마진·강제청산 검증 없이 레버리지를 적용하지 않는다. 테스트 성공은 수익성이나 실제 운영 승인이 아니다.
