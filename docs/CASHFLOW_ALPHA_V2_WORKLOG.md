# Cashflow Alpha v2 작업 기록

- 출발점: PR #3 HEAD `e6a78766aeb4462fc4aeb01f80fcbf24ca7096cb`.
- 신규 브랜치: `research/cashflow-alpha-v2-20261025`.
- 기존 결과와 두 후보의 설계 문서는 변경하지 않는다.
- 실제 주문 및 레버리지 검증은 이 실험에 포함하지 않는다.
- 최초 고정 커밋 `81ba51e` 뒤 실행은 Git의 LF와 Windows의 CRLF 차이를 감지해 계좌 계산 전에 중단됐다. 전략 성과는 생성되지 않았다. 텍스트 코드 해시를 LF로 정규화하고, 기존 고정 파일을 보존한 채 `freeze-portable.json`을 다시 고정한다. 가격·원시 ZIP·실행 결과의 바이너리 해시는 정규화하지 않는다.
