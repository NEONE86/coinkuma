# 바이낸스 선물 4시간봉 연구 — 데이터 게이트 결과

**NEEDS_MORE_EVIDENCE.** 실제 선물 자료의 수집·시점 정합성 감사와 별도 구현의 원시 데이터 재현을 완료했다. 과거 OI 가용시각, 정산 마크가격 및 계약 조건의 필수 증거가 부족해 **전략 성과 백테스트는 실행하지 않았다.** 선물 계좌·주문·마진·강제청산 엔진도 이번 단계에서 구현 완료한 것으로 취급하지 않는다.

## 출발점과 고정

- PR #4 HEAD `e49f3f06518eb3bf4ea59e82680f96c46acf2828`에서 새 브랜치 `research/futures-structure-4h-20261025`를 만들었다. 기존 결과와 구현은 수정하지 않았다.
- [초기 감사 계획](FUTURES_4H_AUDIT_PLAN.json)에 BTCUSDT·ETHUSDT 두 계약, 수집 2021-01-01~2026-09-30, 후보 평가 2022-01-01~2026-09-30을 가격 성과 조회 전에 정했다. 현재 생존 종목 전체에서 과거 승자를 선택한 실험이 아니며, 거래소 전체·알트코인으로 일반화하지 않는다.
- 원시 자료의 초기 가용성 감사 후 [단일 후보 규칙·비용·구간](FUTURES_4H_PROTOCOL.json)을 고정했다. 최초 감사에서 데이터 부족을 이미 관찰했다는 사실도 [freeze.json](experiments/futures-4h-2026-10-25/freeze.json)에 기록한다. 수익률을 본 뒤 문턱을 바꾼 것은 아니며, 수익률 자체를 계산하지 않았다.
- 로컬 고정 커밋 `fef1ad44e604a2efe7ba480e523517bd7755a54a`의 코드·설정 12개 파일을 고정했다. 별도 GitHub 게시 커밋 `eddeda950608d45591e3704842b8f647fbb93f62`와 트리 `2601ea5012fd6fe0f85bc80be663115e3241c8a0`이 동일하다. 게시 시각을 초기 감사 이전 시각이라고 주장하지 않는다.

## 실행한 작업과 보류한 작업

| 단계 | 상태 |
|---|---|
| 공식 선물 ZIP·CHECKSUM·목록 원문 수집 | 완료, 4,416 ZIP·17 목록 페이지 |
| 전체 원시 행·시간 단위·중복·정렬·결측 감사 | 완료, 1,466,397행 |
| 규칙·비용·평가 구간 및 데이터 중단 기준 고정 | 완료 |
| 별도 파서의 원시 바이트·정규화·판정 독립 재현 | 완료 |
| 엄격한 as-of 신호 원형과 펀딩 현금흐름 산술 | 구현·합성 테스트 완료 |
| 완전한 선물 자산회계·체결·마진·강제청산 엔진 | 데이터 자격 확보 전 보류 |
| 실제 전략 PnL·연도별 수익·200개 대조군 | 미실시, 데이터 게이트 미달 |
| 전략 성과의 독립 재현 | 미실시, 원본 성과 실행 없음 |
| 실주문·실계좌·레버리지·PR 병합 | 실행하지 않음 |

결과·출처와 해시는 [결과 보고서](experiments/futures-4h-2026-10-25/RESULTS.md), [독립 검토](experiments/futures-4h-2026-10-25/INDEPENDENT_REVIEW.md), [자료별·연도별 커버리지](experiments/futures-4h-2026-10-25/yearly_source_coverage.csv)에 있다. `sources_BTCUSDT_2021.csv` 등 12개 파일에 모든 ZIP의 URL·원시 ZIP 해시·CHECKSUM 해시·정규화 해시·객체 수정시각을 기록했다. 원시 가격·OI 행은 Git 밖에 보관한다.

## 고정 후보

펀딩·OI 혼잡 후 완성된 가격·체결 수급의 반전을 시험한다. 롱은 평균 확정 펀딩 ≤ -1bp, OI 24시간 증가 ≥ 5%, 양봉·전봉 종가 회복, 테이커 매수 비중 ≥ 55%, 마크/지수 베이시스 ≤ 0이 모두 필요하다. 숏은 반대 방향의 규칙이다. 다음 펀딩 예상치와 이미 확정된 펀딩을 혼동하지 않는다.

UTC 1시간봉 네 개로 완성된 4시간봉만 사용한다. 경계 후 10분에 관찰하고 가장 이른 체결은 다음 4시간 경계다. 초기 2.5 ATR14 스톱, 1~8% 진입 스톱 거리, 추가매수 금지, 최대 72시간 보유 등을 고정했다. 수수료/슬리피지는 기본 편도 5/5bp, 스트레스 10/15bp의 **가정 시나리오**이며 실제 계정 요율을 확인했다는 뜻은 아니다. 실제 정산시각·정산 마크·확정 펀딩율로 비용을 계산해야 한다.

포지션별 위험 0.5%, 총위험 1%, 종목 명목비중 20%, 총명목비중 40%, 차입·레버리지 확대 금지다. 실행 엔진과 장중 갭 대응을 실제 검증하지 않았으므로 이 수치가 유지됐다고 주장하지 않는다. 게이트 통과 뒤에야 가격 중심 대조군과 무작위 200개 계좌, 스트레스 PF > 1.3, 표본 200/50, MDD 15%, 시기 안정성 및 월 경제성 기준을 시험한다.

## 재현

Python 표준 라이브러리만 사용한다. 정확한 원시 바이트 재현에는 보존된 `archive/futures-4h/full/http`와 목록·파일 감사 원장을 사용한다. 공급자의 ZIP은 갱신될 수 있다. 새 수집은 기존 폴더를 덮어쓰지 않는다.

```sh
python -X utf8 -m unittest discover -s tests -v
python -X utf8 -m compileall -q one.py one_trading tests scripts tools
python -X utf8 one.py --help
python -X utf8 one.py status --workspace workspace_futures_check

python -X utf8 scripts/collect_futures_4h.py --output archive/futures-4h-reproduction --workers 32
python -X utf8 scripts/run_futures_4h.py --freeze docs/experiments/futures-4h-2026-10-25/freeze.json --archive archive/futures-4h-reproduction --output output/futures-4h-reproduction
python -X utf8 scripts/validate_futures_4h.py --archive archive/futures-4h-reproduction --run output/futures-4h-reproduction --output output/futures-4h-reproduction/independent.json
python -X utf8 tools/export_futures_4h.py --archive archive/futures-4h-reproduction --run output/futures-4h-reproduction --output output/futures-4h-public-reproduction
```

`--freeze-only`는 새 실험의 사전등록용이며 단순 재현에서는 기존 고정 파일을 다시 만들지 않는다. 고정 코드가 바뀌면 실행기는 거부한다. 기존 v1/v2 결과를 재현할 때는 각 원래 PR의 고정 커밋을 사용한다. 새 연구 파일을 추가한 체크아웃을 원래와 동일한 코드 파일 집합이라고 간주하지 않는다.

## 재개에 필요한 자료

연속된 OI·펀딩·마크/지수·체결 수급의 **원래 수신시각 또는 입증 가능한 과거 가용시각**, 수정 이력 및 출처 해시가 우선이다. 현재 API의 과거 관측시각만으로 이를 대체하지 않는다. 정산 가격·계약/마진 조건도 보완하거나, 비용·청산의 보수적 상한을 사용하는 별도 모델을 새 버전으로 사전등록해야 한다. 지금의 기각·보류 기록은 그대로 유지한다.

수신시각을 기록한 상용/보유 원천의 예로 [Tardis 스키마](https://docs.tardis.dev/downloadable-csv-files/data-types)의 `local_timestamp`가 있다. `funding_rate`는 다음 정산 예상치여서 확정 비용과 별도로 대조해야 한다. [공개 다운로드 규정](https://docs.tardis.dev/downloadable-csv-files)은 무키 샘플을 매월 1일로 설명한다. 이를 연속 수년치 이력으로 확보·검증한 것은 아니며 구독·구매도 하지 않았다. 특정 공급자의 데이터가 자동으로 연구 기준을 통과한다는 추천이 아니다.
