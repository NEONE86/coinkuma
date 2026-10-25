# Cashflow Alpha v3 재현

먼저 [결론·판정](experiments/repricing-v3-2026-10-25/RESULTS.md)을 읽는다. PR #4 HEAD `e49f3f06518eb3bf4ea59e82680f96c46acf2828`에서 시작한 독립 연구다. 기존 기각 결과와 브로커를 수정하지 않았다.

Python 3.10+ 표준 라이브러리만 사용한다. 로컬 실험은 Windows/Python 3.13에서 실행했다. UTF-8 모드를 명시해야 한국어 Windows 기본 cp949로 표현할 수 없는 심볼도 저장할 수 있다.

```sh
python -X utf8 -m unittest discover -s tests -v
python -X utf8 -m compileall -q one.py one_trading scripts tests tools
python -X utf8 one.py --help
python -X utf8 one.py status --workspace workspace_check

python -X utf8 scripts/run_repricing_v3.py --freeze docs/REPRICING_V3_FREEZE.json --archive /path/to/cashflow-v2/market.sqlite3 --output output/repricing-v3
python -X utf8 scripts/validate_repricing_v3.py --freeze docs/REPRICING_V3_FREEZE.json --archive /path/to/cashflow-v2/market.sqlite3 --run output/repricing-v3
python -X utf8 scripts/analyze_repricing_v3.py --run output/repricing-v3
python -X utf8 tools/export_repricing_v3.py --run output/repricing-v3 --output output/repricing-v3-reviewed
```

`output/repricing-v3`는 존재하지 않는 새 디렉터리여야 한다. 기존 동결 JSON을 덮어쓰거나 `--freeze-only`로 새로 만들지 않는다. 실행 전 모든 `one_trading/`, `scripts/`, `tests/` Python 파일, 설정과 원문 프로토콜의 LF 정규화 SHA가 맞아야 한다. 보고서 생성 도구는 계산과 분리되며 가설·계좌 판정을 바꾸지 않는다.

필요한 입력은 기존 756개 시장·844,092개 일봉과 원본 ZIP/체크섬을 담은 SQLite다. 정규화 일봉 SHA-256은 `b139383a4eacb5cf9150354576eb9cf2f44937c9a3e019bacfa88f3e37d2b98f`. 원본을 보유하지 않은 공개 체크아웃은 합성 테스트만 실행할 수 있다. [기존 수집 절차](CASHFLOW_ALPHA_V2.md)로 공식 무료 데이터를 수집할 수 있지만, 이후 공급자 수정으로 동일 해시가 나온다는 보장은 없다. 해시가 다르면 이 실험과 별도 데이터로 기록해야 한다.

행별 아틀라스, 비중복 앵커, 가설별 사건, 매칭 쌍, 모든 계좌 원장과 전체 입력·출력 해시는 Git 밖에 생성된다. 공개 PR에는 검토한 시장별·구간별 집계, 사례 대응표와 재현 소스만 넣는다. 실행은 시장 데이터 읽기와 가상 계좌 계산뿐이며 실주문·계좌 연결·유료 구매를 구현하거나 호출하지 않는다.

동결 로컬 커밋 `affc281`과 `3f7fcc1`은 GitHub 커넥터 게시 커밋 `4693702`와 `de14cc3`에 각각 동일한 Git tree로 옮겼다. 커밋 ID는 작성 시각·작성자 메타데이터가 달라 다르다. 규칙 동결은 실제 결과 생성 전에 로컬에서 이루어졌고, 원격 공개는 그 뒤였다. 관련 내용은 [연구 기록](REPRICING_V3_WORKLOG.md) 및 실행 manifest에 보존했다.

일봉의 장중 경로, 시가 주문장, 실제 부분체결, 완전한 토큰 재명명·이전 공지와 당시 데이터 발표 지연은 이 실험으로 검증되지 않는다. 과거 구간은 다른 연구에서 이미 관찰되었으며 완전히 미관측된 OOS가 아니다.
