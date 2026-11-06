# ONE Zero Discovery v0.1

**실행형 연구 프로토타입: AI가 사전에 지정된 매매법 없이 관측·롱/숏/대기/청산 규칙을 만들어 진화시키는 프로그램.**

- Python 3.10 이상, 외부 패키지·API 키 필요 없음.
- 인터넷·거래소 접속·모델 유료 호출·실제 주문 없음.
- 합성 시장 기능 검증만 수행. 실전 기대수익 **검증되지 않음**.
- 내부 AI 방식은 유전적 프로그래밍이며 LLM·Transformer·MuZero는 아직 미구현.

## Windows CMD/PowerShell 명령 (ZIP 해제한 폴더에서)

```powershell
python -m unittest discover -s tests -p test_discovery_zero.py -v
python -m one_trading.discovery_zero demo --output test_noedge.csv --bars 900 --world noedge
python -m one_trading.discovery_zero demo --output test_hidden.csv --bars 600 --world leadflow
python -m one_trading.discovery_zero run --data test_hidden.csv --source-kind SYNTHETIC_DEMO --output test_hidden_report.json --population 18 --generations 4 --seed 77
python -m one_trading.discovery_zero verify --data test_hidden.csv --report test_hidden_report.json
```

같은 파일이 이미 있다면 덮어쓰지 않고 오류를 내므로 출력 이름을 바꾼다.

실제 과거 시세는 CSV로 제공해야 한다. `docs/DISCOVERY_ZERO_V1.md`에 입력 규격 및 실행법이 있다. 실거래소 연결과 무인 운용은 아직 없다.

## 만든 결과물

- `one_trading/discovery_zero/`: 데이터 감사·정책 표현·시장 재생·진화 탐색·회계 재계산·CLI.
- `tests/test_discovery_zero.py`: 19개 합성 기반 테스트.
- `demo_*_report.json`: 실험 원장 예시. 숫자는 **가상 시장의 성과**다.
- `docs/DISCOVERY_ZERO_V1.md`: 설계, 한계, 후속 개발 계획.

## 성공·실패의 의미

`NO_PROMOTABLE_EDGE`: 현재 입력과 내부 선택 조건에서 현금보다 좋은 연구 후보가 없음. 실패가 아닌 명확한 실험 결론이다.

`RESEARCH_CHALLENGER_POSITIVE`: 합성 또는 과거 시장 내 제한된 기간에서 좋은 연구 후보가 나옴. **절대 실전 투입 승인 아님**.

`PASS_DETERMINISTIC_REPLAY`: 변조되지 않은 입력으로 동일한 판단·체결·장부를 재현.

이 프로그램의 다음 우선순위는 기존 Bybit 실제 수신 원장과 체결 깊이·호가 지연 시뮬레이터를 연결하는 것이다.
