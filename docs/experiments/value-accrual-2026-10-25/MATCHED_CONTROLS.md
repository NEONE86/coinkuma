# 동일 날짜 대조군

| 사건 | 결과를 보지 않고 선택한 대조군 | 별도 실패 전용 기술 대조군 |
|---|---|---|
| ETH-london-12965000 | BNBUSDT, XRPUSDT, ADAUSDT | BNBUSDT, XRPUSDT, DOGEUSDT |
| AAVE-v3-286 | NEARUSDT, RUNEUSDT, AVAXUSDT | NEARUSDT, RUNEUSDT, CAKEUSDT |
| UNI-bravo-93 | AAVEUSDT, HBARUSDT, ZENUSDT | AAVEUSDT, HBARUSDT, ZENUSDT |

동일 D, 동일 과거 30일 유동성 bin·연속 거래이력 bin에서 로그 유동성·이력 거리의 합으로 가까운 3개를 선택했다. 동률은 symbol 순이다. 사건 토큰과 D에 이미 알려진 패널의 181일 노출 토큰을 제외했다. 결과와 실행 여부는 선택에 쓰지 않는다. 3건 모두 3개 대조군이 있어 9개, 같은 종목-날짜 재사용 0개다. 같은 토큰의 다른 날짜나 시장 동조화가 독립성을 보장하지는 않는다.

추가 실패 전용 표는 180일 2배 실패에 조건부인 **사후 기술 비교**다. 이 표에서 성공률·기본확률을 산출하지 않았다. 9개 일반 대조군 중 성공 2개를 삭제하지 않았다. 과거 PR #7의 hindsight 매칭 결과를 재활용하여 확률로 오인하지 않았다.

[matched_controls.csv](matched_controls.csv)에 역할·신호일·거래이력·유동성 bin·국면·거래대금 변화·BTC 상대성과·변동성 변화·라벨을 제공한다. 경제적으로 다른 토큰을 거래 규모와 이력만으로 비교해 생기는 교란은 남는다. 실거래 가격전용 전략과의 계좌 성과 차이가 아니라 사건 후 목표도달률의 기술 비교다. 사건별 원문은 [event_panel.json](event_panel.json), 전체 확률/구간은 [statistics.json](statistics.json)에 있다.
