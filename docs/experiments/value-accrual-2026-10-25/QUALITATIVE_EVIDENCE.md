# 과거 빈티지가 입증되지 않은 근거

아래 페이지는 SHA256과 **현재 수신 시각**을 보존했지만, 현재 내려받은 원문이 표시된 과거 날짜에 공개됐다는 불변 증거가 없다. 역사적 피처·신호일 결정에서 제외했다. [qualitative_sources.json](qualitative_sources.json)에 URL별 해시·수신 시각·페이지 주장 시각·수정 시각이 있다.

| 자료 | 경제적 의미 | 시점 제한 |
|---|---|---|
| [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559), [London 공지](https://blog.ethereum.org/2021/07/15/london-mainnet-announcement) | 기본 수수료 소각 설계와 업그레이드 계획 | 문서 작성일·수정 공지를 과거 피처로 사용하지 않음. 활성화 블록만 체인 사건으로 분리. 소각을 순공급 감소로 치환하지 않음. |
| [UNIfication](https://blog.uniswap.org/unification) | 수수료 귀속·토큰 sink 이전·성장 예산 계획 | 2025-11-10 표시 날짜의 정확한 원문 빈티지 미입증. 12월의 onchain 제안 원문과 별개. 11월 발표 전후 가격을 임의로 연결하지 않음. |
| [Aavenomics Part One](https://governance.aave.com/t/arfc-aavenomics-implementation-part-one/21248) | 매입·분배·보상 구조 제안 | API 첫 글 created 2025-03-04, updated 2025-03-14. 현재 본문을 3월 4일 가용한 것으로 소급하지 않음. |
| [Aave DAO Funding Insights](https://governance.aave.com/t/aave-dao-funding-insights/24192) | 예산·매입·수익 보고와 변경 제안 | created 2026-02-28, updated 2026-03-24. 작성자의 사후 보고와 당시 실현 거래를 구분. |
| [Pause AAVE Buybacks](https://governance.aave.com/t/arfc-pause-aave-buybacks/24686) | 매입 중단이라는 부정적 정책 근거 | 2026-04-22 작성/수정 시각이 같아도 현재 페이지의 과거 불변 바이트를 입증하지 못함. 온체인 실행 사건으로 가공하지 않음. |

이용자 수요·수수료·프로토콜 매출·실현 토큰 보유자 귀속·발행/언락·순자본 유입을 완전한 수치 패널로 확장하지 못했다. 주소 수는 사용자 수가 아니며, LP 수수료는 보유자 매출이 아니고, TVL/거래대금 증가는 순자본 유입이 아니다. 이번에 확인한 경제적 실행은 정책/allowance/sink 이전이며 지속적인 현금흐름의 검증이 아니다.

선택 패널에는 검증 가능한 부정적 가치귀속 변경이 부족하다. 중단·실패·거절·희석 사건의 누락 가능성은 분모 편향이며, 성공률 해석을 제한하는 주요 이유다.
