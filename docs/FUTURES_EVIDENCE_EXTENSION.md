# PR #5 후속: 과거 가용시각·정산·계약 증거 보완

**NEEDS_MORE_EVIDENCE.** [한국어 결과 보고서](experiments/futures-evidence-extension-2026-10-25/RESULTS.md)에 실제 추가 확보 자료와 남은 누락을 기록했다. 이전 실험의 결과·소스·고정 규칙은 보존했다. 이번 독립 재현은 데이터·판정에 한정되며 전략 성과 재현은 미실시다.

## 재현

Python 표준 라이브러리만 사용한다. `archive/futures-evidence-extension`의 HTTP 바이트 캐시와 정규화 커넥터 응답, `archive/futures-4h/full`의 원래 공식 ZIP 캐시를 보존해야 정확한 원문 재현이 된다. 새 다운로드는 공급자가 갱신한 파일일 수 있으므로 기존 폴더를 덮어쓰지 않는다.

```sh
python -X utf8 scripts/collect_futures_evidence.py --output archive/futures-evidence-reproduction
```

이 수집기는 Tardis 공개 샘플·문서만 받는다. Binance 도구의 12개 정규화 요청/응답을 새 디렉터리의 `connector_probes.json`에, 공식 5월 공지 웹 추출물을 `terms_web_extract.txt`에 보존해야 다음 실행이 가능하다. 기존 보존본은 `archive/futures-evidence-extension`에 있다. 커넥터 요청 인자는 공개 `funding_samples.json`에 있다. 다음 형태로 도구 호출 전후 시각과 응답을 기록한다.

```json
{"capture_format":"Normalized MCP; not upstream HTTP", "requests":[
  {"status":"fulfilled","value":{"kind":"funding_history","day":"2022-01-01",
    "params":{"symbol":"BTCUSDT","startTime":1640995200000,"endTime":1641081599999,"limit":10},
    "started_utc":"<actual request start>","finished_utc":"<actual request end>",
    "result":{"structuredContent":{"result":["<actual API row objects>"]},"isError":false}}}
]}
```

이는 기록 형식의 설명이다. 예시의 자리표시자를 실데이터로 넣지 않는다. 공급자의 원래 HTTP 바이트를 확보했다는 주장은 하지 않는다. 정상 요청과 최초 gzip 헤더 누락으로 인한 406은 별도 캐시로 보존한다. 정상 원시 요청 식별자는 URL에 `|Accept-Encoding:gzip`을 더해 해시화한다. 시장 관측 행은 공개 리포지토리에 넣지 않는다.

```sh
python -X utf8 scripts/run_futures_evidence.py --archive archive/futures-evidence-extension --prior-archive archive/futures-4h/full --output output/futures-evidence-new-run
python -X utf8 scripts/validate_futures_evidence.py --archive archive/futures-evidence-extension --prior-archive archive/futures-4h/full --run output/futures-evidence-new-run
python -X utf8 tools/export_futures_evidence.py --run output/futures-evidence-new-run --output output/futures-evidence-public

python -X utf8 -m unittest discover -s tests -v
python -X utf8 -m compileall -q one.py one_trading tests scripts tools
python -X utf8 one.py --help
python -X utf8 one.py status --workspace workspace_futures_evidence_check
```

실행 출력에는 새 폴더가 필요하다. 공개 `SHA256.json`은 CRLF→LF 정규화 후 공개 파일의 해시이며, HTTP 원문 해시는 실제 수집 바이트의 해시다. `verification.json`의 새 코드 해시는 검증 후 참조값이다. 추가 파서를 모두 수집 전에 고정했다는 주장이 아니다. 추가 표본일과 판정 기준은 수집 전 커밋에 기록했고 전략 규칙은 PR #5의 고정을 유지했다.

## 다음에 해소할 사항

1. 원래 평가 1,734일과 24시간 전 참조에 필요한 연속 raw OI·구조 자료를 확보한다. 공급자의 원래 수신시각, 스키마/구현 버전, 결측·수정 이력을 감사한다.
2. 실제 모든 정산시각의 확정 펀딩과 정확한 정산 마크, 신호 시점의 확정율 가용시각을 확보한다. 누락된 2022~2023년도 대상에서 빼지 않는다.
3. USD-M 무기한 두 계약의 유효시점별 가격·수량 필터, 증거금, 계약 동일성·종료 조건을 검증한다. 기존 포지션 예외와 변경 적용 창도 보유 중 처리해야 한다.
4. 위 자격 충족 후 고정 전략의 완전한 선물 자산회계·체결·마진·강제청산 엔진, 기본/스트레스 성과와 독립 재현·대조 실험을 수행한다.

유료 데이터 구매나 추가 권한 제공을 이번 결과에 포함하지 않았다. 무료 표본은 출처 실재성과 시각 구조 확인에 유용하지만, 연속 자료를 대체하거나 충분한 거래 표본을 제공하지 않는다.
