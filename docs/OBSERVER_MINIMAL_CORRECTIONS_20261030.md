# 공개 재시험 후 최소 교정

기준은 `07de3c0f2e0ca8a5070e03acc6aa3b6111140e57`이다. 기존 checkout/runtime은 수정하지 않고 `fix/observer-minimal-corrections-20261030`에서 교정했다. 이 작업은 로컬 시험·과거 raw 재생만 승인되었다. 새 시장 수집, 모델/채널, 원격 게시, 스케줄러 변경은 실행하지 않는다.

이전 5분에는 metadata 1 / mini 300 / closed bar 25, 13,024,093 bytes를 보존했지만 accepted는 metadata 1뿐이었다. 사건·판단·모의 체결·실제 알림·모델 호출은 0이었다. 따라서 수신·보존 확인이며 실자료 수직 경로 완성이 아니다. 독립 검토의 Observer 65 PASS와 비동기 2개 종료 문제, 전체 208개 runtime 위치 제약은 과거 검토 결과다. 이번 실행 로그와 혼동하지 않는다. runtime 안전 가드는 유지한다.

## 결함과 변경

| 결함 | 변경 파일·함수 | 검증 범위 |
|---|---|---|
| 시계 불일치·프레임 전체 격리 | `clocks.py` capture/calibration/interval/fresh, `observe.py` parse_revision, `engine.py` ingest | 왕복 시간 전체를 비대칭 범위로 사용; source/wall/monotonic/session 분리; symbol 단위 판정; 원문 불변; 처리 완료 이후에만 사용 |
| 위험 결정 후 outbox 누락 | `notify.py` risk_outbox, `engine.py` evaluate | 결정과 outbox 한 transaction; 현재 위험 원인·수량·근거·revision; 현재 위험 관측부터 지연 측정 |
| 대기 REDUCE가 EXIT를 가림 | `engine.py` evaluate/execute, `paper.py` fill | append-only 취소/대체; 최신 재고·위험 재검사; 새 EXIT도 자체 2초 지연; 중복 체결 금지 |
| 만료 승인 때문에 ACK 복구 실패 | `transport.py` recover_deliveries/send_webhook | 저장된 ACK만 로컬 복구, 네트워크 0; 새 전송은 별도 승인; UNKNOWN blind retry 금지 |
| 실패 시 수신 바이트가 0으로 사라짐 | `transport.py` public_get, `store.py` raw_usage, `accounting.py` snapshot | 64 KiB bounded read; 알려진 partial 보존; unknown 잔여 상한 예약; HTTP/WS 공유 cap·중복 제거 |
| 장기 예산/accepted 경로 부하 미검증 | `transport.py` smoke, `store.py` 파생 인덱스, `scripts/benchmark_sentinel.py`, `scripts/sentinel_budget.py` | cap에서 중단·공백 기록; 1h/24h 합성 누적 부하; 수신·보관·요청·pin 별도 계량 |

시계 가정은 RTT ≤2초, 표본 수명 120초, drift 50 ppm, wall/mono 점프 50ms이다. 시장 결과에 맞춘 보정값이 아닌 고정된 공학적 가정이며 실제 호스트에서 추가 확인해야 한다. 서버 시각은 요청/응답 사이에 찍힌다는 가정과 ms 양자화를 명시한다. 범위 경계의 항목은 UNCERTAIN, 상한을 넘으면 FUTURE이다. 새 프로세스 UUID의 monotonic epoch를 이전 것과 비교하지 않는다. 오래된 실제 자료에는 교정 영수증이 없으므로 UNVERIFIABLE이며 사후 보정하지 않는다. 판정 시점의 처리 지연까지 freshness와 실행 지연에 포함한다.

## 데이터 계약과 예산

| 계층 | 주소/방식 | 범위·주기 | 수신/보관/요청 의미 |
|---|---|---|---|
| 전체 얕은 관측 | `stream.binance.com:9443`, `!miniTicker@arr`, WS | 변경된 전체 symbol, 약 1초 | rolling 24h v/q; 누락 symbol은 상장폐지 아님; 원문 envelope 보존 |
| 제한 deep | 같은 WS, `<symbol>@kline_1m` | 최대 5종목; 완성된 1분 봉만 판단 입력 | 미완성 메시지도 수신 회계에 포함; 1분 거래량 근거는 closed bar |
| 상태 metadata | `api.binance.com/api/v3/exchangeInfo?showPermissionSets=false`, GET | 300초, 288/일; 모든 status 보존 | TRADING 필터 없음; suspended→TRADING 관측 가능; 마지막 상태와 연결 |
| 시계 | `api.binance.com/api/v3/time`, GET | 60초, 1440/일; 1 KiB 상한 | request/receive timing 및 RTT 보존; 새 승인에서만 실행 |
| 실행 가능 호가 | `api.binance.com/api/v3/depth?symbol=…&limit=20`, GET | 필요 시, 최대 400/일; 64 KiB 상한 | 실제 수신 후 delay/freshness/잔고/수수료·슬리피지 재검사 |

`meter.json`의 known received application bytes, HTTP identity body, WS decoded payload, uncertain upper-bound charge, physical retained bytes, HTTP attempts, delivered WS messages, evidence pins를 분리한다. TLS/압축 wire 바이트는 측정하지 않았고 null로 표시한다. 저장 압축·동일 raw dedup은 수신 cap을 줄이지 않는다. OPEN/미감사 증거 pin은 자동 삭제하지 않는다.

이전 300초 raw로 환산하면 metadata 1,953,528,768 + WS 1,797,410,016 = **3,750,938,784 bytes/일**이다. 256 MiB는 약 **1.718시간**에 소진한다. 이전 계량에서 버렸던 ACK/미완성 메시지나 새 clock traffic은 이 환산에서 빠지므로 하한 성격이다. 실제 24시간 측정이 아니다.

범위 축소안(매시간 2분 WS + 시간당 전체 status metadata)은 약 223 MB/일이지만 매시간 58분이 비고 상태 변경 감지가 늦다. 목표에 필요한 커버리지를 잃으므로 추천하지 않는다. 커버리지 유지안으로 **8 GiB application bytes/일, HTTP 총 2,500회/일**의 별도 승인 검토를 추천한다. metadata 288 + clock 1440 + book 최대 400 = 2128, 복구 여유 372회다. 이는 승인/적용된 한도가 아니다. 기존 256 MiB, 총 1000/활성 688/복구 예약 312, book 400 한도는 그대로다. 필요 용량은 새 실측 후 다시 평가한다.

cap 도달/예약 부족 시 `observation_halt`에 사유·공백·명시적 재개 조건을 남기고 transport를 종료한다. 반복 재접속으로 쿼터를 소모하지 않는다. 미결 read는 재시작 후에도 예약이 유지되며 비용을 모른다는 이유로 0으로 정산하지 않는다.

## 재현과 독립 검증

Python 3.13 Windows에서 실제 실행한 정확한 결과와 소스 hash는 배포 ZIP의 `REPORT_KO.md`, `identity.json`, 로그가 기준이다. 다음 명령은 네트워크를 호출하지 않는다. runtime 출력 경로는 새 경로를 사용하고, 저장소 안전 가드가 있는 시험은 Git 밖 임시 디렉터리를 사용한다.

```sh
python -B -m unittest discover -s tests -p "test_sentinel*.py" -v
python -B -m unittest discover -s tests -v
python -B scripts/replay_sentinel_corrections.py --original ../original/actual --out ../fresh-replay
python -B scripts/verify_sentinel_corrections.py ../fresh-replay/real_reused/export
python -B scripts/benchmark_sentinel.py --seconds 86400 --out ../fresh-load
python -B scripts/sentinel_budget.py ../original/actual
```

`test_sentinel_corrections.py`의 13개 새 회귀를 먼저 구버전에 실행했다(5 failures, 8 errors; 새 API 부재 포함). 이후 경계 회귀는 `test_sentinel_boundaries.py`에 추가했다. 원래 audit probe와 새 API에 맞춘 probe 및 diff를 모두 보존한다. 변경된 probe는 다음 실행 가능한 book(새 EXIT 결정 2초 이후)을 추가하고, 혼합 프레임에는 실제 경로의 parse_revision을 사용한다. 원래 strict primitive parse의 의미를 약화시키지 않는다.

독립 검증기는 표준 라이브러리만 사용하고 구현 모듈을 import하지 않는다. chain/raw/source 계약, clock 판정, 입력 가용시각, 위험 outbox 누락·수량·근거, 취소 우선순위, Decimal 수수료·재고·성과, unknown 예약을 재계산한다. 재해시한 clock/risk/timeout 변조자료도 거부하는지 확인한다. 합성 검증과 old REAL_REUSED 표본은 새 전향 실적이 아니다.

부하 시험은 모든 입력을 accepted 경로로 처리하는 **가속 합성** 시험이다. 별도 fixture에서만 8 GiB를 허용하며 실제 정책 cap은 변경하지 않는다. 120 mini 항목/초, metadata 775 symbol/300초, 5 closed bar/분을 사용한다. 실제 이전 run은 1145개 고유 mini symbol을 관측했으므로 이 부하 시험이 전체 시장 worst case를 증명하지 않는다. 생성기·정확한 버전·1h/24h milestone·원장/원문 크기·지연·backlog를 제출한다. 대용량 생성 runtime 전체는 ZIP에 중복하지 않고 재생성 명령과 표본/계산 결과를 담는다. TLS/WS 버퍼, 실제 종일 수집, 장애율, 사건 빈도, 실제 모델 성능은 측정하지 않았다.

## 남아 있는 외부 단계

최종 경계 점검에서 문자열이 아닌 symbol의 파생 인덱스 binding 오류와 독립 검증기의 동일 경계를 추가 교정했다. 해당 잘못된 항목은 원장에 INVALID로 남고 문자열 symbol 인덱스에 넣지 않는다. 정상 symbol의 처리 의미는 유지한다. 24h 부하 결과는 이 마지막 경계 수정 직전 `b3bcc09`의 exact source와 함께 보존하며, 마지막 수정본은 별도 1h 부하와 전체 회귀로 검증한다. 두 소스의 측정 결과를 같은 최종 버전의 24h 실행으로 표기하지 않는다.

`MARKET_ACCESS_REOPEN_V1`은 단일 미검증 가설 그대로다. 가격변화에 따른 deep 승격은 투자 기회나 AI 판단 완료가 아니다. slow_economic, 실제 모델 provider, 일반 NEW 후보 생성은 미연결이다. 다음 5분 시험은 ZIP의 정확한 source/policy/account hash, 대상·기간·상한·채널 없음이 명시된 **미승인 요청안**만 준비한다. 실제 qualifying 사건이 없으면 그 경로는 미검증으로 남긴다. 채널 outbox/합성 ACK/실제 provider ACK/휴대폰 수신을 구별한다. 실자료 수익성이나 24시간 운영 성공을 주장하지 않는다.

공식 참고: [Binance REST general](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general), [Python monotonic](https://docs.python.org/3/library/time.html#time.monotonic), [IncompleteRead](https://docs.python.org/3/library/http.client.html#http.client.IncompleteRead).
