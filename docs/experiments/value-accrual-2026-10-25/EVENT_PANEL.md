# 사건 패널과 원문 증거

**정확한 원문 버전의 체인 공개**와 **이번 수집기의 수신**을 구분한다. 과거의 오프체인 최초 발표·수신이 입증됐다는 뜻이 아니다.

| 사건 | 체인 공개 (UTC) | 이 수집기의 최초 영속 수신 (UTC) | 경제적 실행 (UTC) | 메커니즘 |
|---|---|---|---|---|
| UNI-bravo-102 | 2026-09-25T16:06:59.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | FEE_ROUTING_EXPANSION |
| UNI-bravo-100 | 2026-07-17T20:34:35.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | V4_PROTOCOL_FEE_ACTIVATION |
| UNI-bravo-99 | 2026-07-17T20:20:47.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | FEE_ROUTING_EXPANSION |
| UNI-bravo-96 | 2026-05-22T17:30:47.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | FEE_ROUTING_EXPANSION |
| UNI-bravo-95 | 2026-02-25T22:39:59.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | FEE_ROUTING_EXPANSION |
| UNI-bravo-94 | 2026-02-24T19:48:23.000000Z | 2026-10-25T10:06:45.437088+00:00 | 미입증 / null | FEE_ROUTING_EXPANSION |
| UNI-bravo-93 | 2025-12-18T07:43:59.000000Z | 2026-10-25T10:06:45.437088+00:00 | 2025-12-27T20:33:11.000000Z | FEE_ROUTING_AND_TREASURY_SINK_TRANSFER |
| AAVE-v3-286 | 2025-04-04T17:09:35.000000Z | 2026-10-25T10:15:11.034188+00:00 | 2025-04-09T17:59:59.000000Z | BUY_AND_DISTRIBUTE_ALLOWANCE |
| ETH-london-12965000 | 2021-08-05T12:33:42.000000Z | 2026-10-25T10:09:38.806620+00:00 | 2021-08-05T12:33:42.000000Z | BASE_FEE_BURN_ACTIVATION |

16개 후보 전체는 [event_panel.csv](event_panel.csv), 원문 SHA256·블록·트랜잭션·log index·영수증 해시는 [event_panel.json](event_panel.json)에 있다. 해시 없는 임의 요약을 피처로 사용하지 않는다. `historical_collector_first_seen_at=null`은 의도한 결측이다. 정성 자료도 수집 SHA256은 갖지만, 그 해시가 역사적 공개 시각을 증명하지는 않는다.

UNI는 세 governor의 모든 ProposalCreated와 queue/execute/cancel 로그를 열거했다. 원문 ABI를 직접 복호화한 뒤 14개 키워드 후보를 검토하여 7개 직접 정책과 7개 단순 언급을 구분했다. Foundation 지출의 'burn rate', 미래 fee switch를 언급한 법인 설립·거버넌스 문턱 변경은 직접 사건이 아니다. 취소·미실행 제안을 후보 목록에서 삭제하지 않았다. 한 비후보 Alpha 원문에 잘못된 UTF-8이 있어 원시 바이트 해시를 유지하고 대체 문자 표시는 따로 기록했다.

Aave GovernanceV3 생성 537개·실행 507개·취소 26개와 mainnet payload 생성 478개·실행 428개를 수집했다. 이는 2026-10-25 수집 시점의 이력이며 2026-10 이후 정보는 과거 selector에 사용하지 않는다. 후보 범위는 동결된 파일명 조건으로 제한된다. 실행 뒤 삭제된 소스는 공식 diff 도입 커밋에서 복원했다. CID 원문을 해시 검증한 #286만 직접 매입 allowance 사건으로 연결됐다. generic funding update, Snapshot-only 정책, 파일명에 검색어가 없는 정책을 포괄하지 못하므로 Aave 사건 전수조사가 아니다. [coverage.json](coverage.json)과 [UNI 전수 제안 목록](uni_proposal_inventory.json)을 함께 읽어야 한다.

## 증거 수준

Blockscout 블록/거래/원문 로그와 Routescan JSON 영수증의 주소·topics·data·log index·블록 해시·블록 번호·성공 상태·시각을 일치시켰다. Aave IPFS CID는 평문 SHA256으로 오인하지 않고 단일 UnixFS DAG-PB 노드의 multihash를 재계산했다. ETH는 London 직전/활성화 블록의 부모 해시 연결과 base fee 변화를 확인했다. 두 탐색기의 일치는 원본 일관성 증거이며 자체 Ethereum 노드 검증·영수증 Merkle proof·역사적 네트워크 최초 수신 증명은 아니다.

Aave의 onchain ProposalExecuted는 dispatch일 수 있으므로 실제 경제 설정은 PayloadExecuted로 추적했다. UNI 목적지 체인 실행을 수집하지 않은 경우에는 L1 실행을 경제적 완료 시각으로 쓰지 않았다. ETH는 실행 시점 사건만 채택했으며 EIP 작성일이나 London 수정 공지를 이전 시점 피처로 승격하지 않았다.

원문 바이트 328개(약 19.4MB), 요청/수신 영수증은 로컬 증거 묶음에 보관한다. Git에는 검토한 파생 패널과 원문 위치·해시만 제공한다. [source_catalog.json](source_catalog.json)은 해당 원문을 식별하며 원시 가격이나 HTTP 응답 전체를 포함하지 않는다.
