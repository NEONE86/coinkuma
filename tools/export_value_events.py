"""Reviewed public report export; never changes source panel or research results."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from one_trading.value_evidence import EvidenceStore,save_json


def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def pct(v):return '—' if v is None else f'{v*100:.2f}%'
def date(ms):return datetime.fromtimestamp(ms/1000,timezone.utc).strftime('%Y-%m-%d')
def yes(v):return '예' if v else '아니오'
def csv_write(path,rows):
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)


def export(evidence,study,reproduction):
    out=ROOT/'docs/experiments/value-accrual-2026-10-25';out.mkdir(parents=True,exist_ok=True)
    es=EvidenceStore(evidence,offline=True);events=read(evidence/'event_panel.json');summary=read(study/'summary.json')
    stats=read(study/'statistics.json');pairs=read(study/'matched_pairs_private.json');schedule=read(study/'scheduled_events.json')
    review=read(study/'independent_review.json');repro=read(reproduction/'reproduction.json')
    inv=read(evidence/'inventory.json');execution=read(study/'execution_context_private.json')
    def md(name,text):(out/name).write_text(text.strip()+'\n',encoding='utf-8',newline='\n')
    save_json(out/'event_panel.json',events)
    for name in ('summary.json','statistics.json','scheduled_events.json','collector_schedule.json','independent_review.json','run_manifest.json'):
        save_json(out/name,read(study/name))
    save_json(out/'reproduction.json',repro)
    sources=[dict(url=u,sha256=s['sha256'],bytes=s['size'],first_seen_at=s['first_seen_at'],
                  receipt_kind='CURRENT_COLLECTOR_UTC_NOT_HISTORICAL_OBSERVATION') for u,s in sorted(es.sources.items())]
    save_json(out/'source_catalog.json',sources)
    csv_write(out/'event_panel.csv',[dict(event_id=e['event_id'],symbol=e['symbol'],direct_value_accrual=e['direct_value_accrual'],
        title=e['title'],published_at=e['published_at'],first_seen_at=e['first_seen_at'],executed_at=e['executed_at'],
        governance_executed_at=e.get('governance_executed_at'),mechanism=e['mechanism'],execution_scope=e['execution_scope'],
        source_text_sha256=e['source_text_sha256'],timing_status=e['timing_status']) for e in events])
    inventory=[]
    for governor,group in inv['uni'].items():
        for r in group['uni_created']:
            inventory.append(dict(protocol='UNI',governor=governor,proposal_id=r['proposal_id'],
                published_at=r['block_timestamp'],candidate=r['candidate'],valid_utf8=r['valid_utf8'],
                content_sha256=r['description_sha256'],block_number=r['block_number'],tx_hash=r['transaction_hash']))
    save_json(out/'uni_proposal_inventory.json',inventory)
    coverage=dict(uni_governor_counts={n:{k:len(v) for k,v in group.items()} for n,group in inv['uni'].items()},
        aave_log_counts={k:len(v) for k,v in inv['aave'].items()},payload_log_counts={k:len(v) for k,v in inv['payloads'].items()},
        aave_paths=inv['aave_paths'],aave_trees=inv['aave_trees'],aave_candidate_documents=len(inv['aave_documents']),
        ipfs_resolution=[{k:v for k,v in r.items() if k!='text'} for r in read(evidence/'ipfs_resolution.json')['ipfs']],
        proof_errors=read(evidence/'proof_errors.json'),scope='selected_three_protocol_feasibility_cohort_not_all_market_events')
    save_json(out/'coverage.json',coverage)
    qualified=[]
    for u,s in es.sources.items():
        if any(host in u for host in ('blog.ethereum.org/','eips.ethereum.org/','blog.uniswap.org/','governance.aave.com/t/')):
            created=updated=None
            if u.endswith('.json'):
                first=es.json(u)[0]['post_stream']['posts'][0];created=first.get('created_at');updated=first.get('updated_at')
            qualified.append(dict(url=u,source_sha256=s['sha256'],first_seen_at=s['first_seen_at'],
                claimed_public_at=created,current_version_updated_at=updated,classification='QUALITATIVE_ONLY',
                reason='No immutable historical preimage/publication proof for this fetched page version. A displayed creation date, including equal created/updated dates, is insufficient.'))
    save_json(out/'qualitative_sources.json',qualified)
    matched=[];labels=[]
    def normalized(event_id,r,role):
        return dict(event_id=event_id,role=role,symbol=r['symbol'],signal_day=date(r['day']),
            liquidity_bin=r['liquidity_bin'],age_bin=r['age_bin'],history_days=r['history_days'],regime=r['regime'],
            relative28=r['relative28'],volume_ratio=r['volume_ratio'],tr_ratio=r['tr_ratio'],
            **{k+'_delayed_hit':r['delayed'][k]['hit'] for k in ('90d_2x','90d_3x','180d_2x','180d_3x')},
            delayed180d2x_mae=r['delayed']['180d_2x']['mae'],delayed180d2x_days=r['delayed']['180d_2x']['days'],
            delayed180d2x_stop_first=r['delayed']['180d_2x']['stop_before_or_same_bar'],
            delayed180d_end_return=r['delayed']['180d_end_return'],entry_gap=r['entry_gap'],
            broker_gap_acceptable=r['broker_gap_acceptable'],final_history_status=r['final_history_status'])
    for p in pairs:
        labels.append(dict(event_id=p['event_id'],symbol=p['case']['symbol'],signal_day=date(p['case']['day']),
            research=p['case']['research'],delayed=p['case']['delayed'],entry_gap=p['case']['entry_gap'],
            broker_gap_acceptable=p['case']['broker_gap_acceptable']))
        matched.append(normalized(p['event_id'],p['case'],'EVENT'))
        matched.extend(normalized(p['event_id'],c,'OUTCOME_BLIND_CONTROL') for c in p['controls'])
        matched.extend(normalized(p['event_id'],c,'FAILURE_ONLY_DESCRIPTIVE') for c in p['failure_only_controls'])
    csv_write(out/'matched_controls.csv',matched);save_json(out/'event_labels.json',labels)
    save_json(out/'execution_context.json',[dict(event_id=r['event_id'],signal_day=date(r['day']),usable=r['usable'],
        independent_observation=False,delayed=r['atlas']['delayed'] if r['atlas'] else None,
        entry_gap=r['atlas']['entry_gap'] if r['atlas'] else None,
        broker_gap_acceptable=r['atlas']['broker_gap_acceptable'] if r['atlas'] else None) for r in execution])
    table='\n'.join('| '+p['event_id']+' | '+date(p['case']['day'])+' | '+yes(p['case']['delayed']['180d_2x']['hit'])+
        ' | '+str(p['case']['delayed']['180d_2x']['days'] or '—')+' | '+pct(p['case']['delayed']['180d_2x']['mae'])+
        ' | '+yes(p['case']['delayed']['180d_2x']['stop_before_or_same_bar'])+' | '+pct(p['case']['delayed']['180d_end_return'])+' |' for p in pairs)
    eras='\n'.join(f"| {era} | {v['180d_2x']['events']} | {pct(v['180d_2x']['success_fraction'])} | {pct(v['180d_2x']['control_success_fraction'])} | {pct(v['180d_2x']['paired_success_difference'])+('p' if v['180d_2x']['paired_success_difference'] is not None else '')} |" for era,v in stats.items())
    md('RESULTS.md',f'''# 토큰 가치귀속 사건 패널 — NEEDS_MORE_EVIDENCE

**검증된 우위:** 없음. 원문·시각의 일관성과 재현은 통과했지만 예측 우위를 입증하지 못했다.

**실패한 가설:** “가치귀속 제안이면 곧 매매 가능한 재평가 우위다”라는 해석은 지지되지 않는다. 선행 공개 시점의 3개 사건 중 180일 2배 도달은 1개이며 그 1개도 목표 전에 고정 손절선을 먼저 건드렸다. 이는 작은 사후 표본의 기술 결과이고 일반적인 경제 가설의 최종 기각은 아니다.

**다음 단 하나의 행동:** 10개 이상 토큰의 제안·취소·중단·실행을 포함하는 사전 등록 전향 관측 패널을 수집하여, 실제 수신 영수증을 가진 180일 성숙 사건을 확보한다. 이번 PR은 감시 작업을 예약하거나 새 매매 규칙을 실행하지 않았다.

PR #7 HEAD `35bfbbc2a71aa47579974d15e9d1c62e3ba7db55`에서 독립 분기했다. 기존 결과·전략·원장을 수정하지 않았다. 분석은 이미 본 아틀라스를 사용한 선택된 ETH/AAVE/UNI 타당성 조사이며 미관측 OOS가 아니다.

## 시점 증거와 표본

16개 후보 사건의 원문 해시와 체인 공개시각을 검증했다. 9개는 직접 가치귀속 정책이고 7개는 단순 언급이다. 중복 5개, 최근 미성숙 1개를 제외하면 독립 사건은 **3개**다. 2019–2022 개발 1개, 2023–2024 검증 **0개**, 2025–2026-09 도전 2개다. 전 시장 사건의 누락률을 알 수 없어 이벤트 빈도나 전체 시장 성공확률로 일반화할 수 없다.

`published_at`은 **정확한 제안 원문이 체인에 포함된 시각** 또는 London 활성화 블록이다. 최초의 오프체인 발표 시각이라는 뜻은 아니다. `first_seen_at`은 이 증거 수집기의 실제 2026-10-25 수신 시각이며 과거로 소급하지 않았다. 과거 수집기 수신 증거는 없다. 실제 수집 시각을 적용하는 엄격 모드에서는 성숙 표본이 **0개**다. 체인 공개 + 고정 1시간의 재구성 모드만 아래 탐색적 비교에 썼다. 1시간은 실측 지연이나 확정성 보장이 아니다.

## 선행 공개 시점, D+2 기준

| 사건 | 완료 신호일 D (UTC) | 180일 2배 | 도달 일수 | 도달 전/전체 MAE | 손절 선행·동일 봉 | 180일 말 가격변화 |
|---|---|---:|---:|---:|---:|---:|
{table}

마지막 열은 비용·포지션 관리 없는 정규화된 가격 라벨이며 계좌 수익률이 아니다. 실패의 MAE는 전체 관측 창의 최대 역행이다. 2배 도달까지의 intraday 순서는 일봉으로 알 수 없어 손절 우선으로 처리했다. 90/180일 × 2/3배와 진입 갭은 [event_labels.json](event_labels.json)에 모두 있다.

**3개 라벨이 모두 체결 가능한 거래라는 뜻은 아니다.** UNI 발표의 D+2 시가 갭은 +7.22%로 기존 브로커의 +3% 상한을 넘어서 진입 거부 대상이다. 실행 확인 시계에서도 +4.42%로 거부된다. ETH(+2.28%)와 AAVE(+0.33%)는 갭 조건 안에 있지만 두 발표 기준 경로 모두 손절선이 먼저 닿았다. 수수료 없는 원시 가격 라벨을 비용 반영 수익으로 바꾸지 않았다.

## 동일 날짜 대조군과 불확실성

| 구간 | 사건 수 | 사건 180일 2배 비율 | 대조군 비율 | 사건−대조군 차이 |
|---|---:|---:|---:|---:|
{eras}

전체 1/3 대 2/9, 차이 +11.11%p는 우위 판정 근거가 되지 않는다. 사건 성공률 Wilson 95% 참고구간은 **6.15–79.23%**, 사건 단위 부트스트랩 차이는 **−33.33~+66.67%p**다. 후자는 3개 사건만 재표집한 민감도 수치이며 유효한 군집 추론 구간이 아니다. 토큰·반기 군집이 각각 3개뿐이므로 확인적 군집 구간을 산출하지 않았다. 검증 구간 0건이고 동결된 최소 표본·군집 기준에 미달한다.

이 3개 날짜의 과거 적격 시장은 666 종목-날짜, 180일 2배 라벨은 196개(29.43%)다. 사건 중 실패 비율(FDR)은 2/3이다. 해당 날짜로 조건부 제한한 신호 오탐률은 2/470=0.43%지만, 신호가 3개뿐인 선택 패널의 수치다. 전 시장 FPR·독립 표본 확률로 해석할 수 없다. 다른 토큰의 미수집 근본 사건도 통제하지 못했다.

## 실행 확인은 별도 시계

Aave #286의 거버넌스 dispatch는 **2025-04-08 17:59:23 UTC**, payload 실행은 **2025-04-09 17:59:59 UTC**였다. 원장 로그는 4,000,000 aEthUSDT allowance 설정을 보여주며, AAVE 매입 체결이나 소각의 증거가 아니다. 실행 확인 후 D+2 가격 라벨은 40일 만에 2배, MAE −1.61%, 고정 손절 선행 없음이었다. 공개 시점의 66일·−25.22%와 크게 다르지만, **사전 지정한 진단 1개**이며 이를 보고 실행 진입 전략으로 교체하지 않았다.

UNI #93은 2025-12-27 20:33:11 UTC의 1억 UNI `0xdead` 이전을 영수증에서 확인했다. 이는 ERC20 `totalSupply` 감소나 앞으로 발생할 순수익을 입증하지 않는다. 4천만 UNI 베스팅 허용도 함께 있어 순공급 효과를 소각 숫자로 대체하지 않았다. 다른 체인의 fee rollout은 L1 dispatch만 확인했으므로 실제 목적지 `executed_at`은 null로 남겼다. 실행 진단은 [execution_context.json](execution_context.json)에 보존했고 독립 관측 수에 더하지 않았다.

## 판정과 검증

**NEEDS_MORE_EVIDENCE. 연구 우위 없음, 실거래 적격 없음.** 새 매매 가설·계좌 백테스트를 만들지 않았으므로 새 CAGR/PF/월 현금흐름을 주장하지 않는다. PR #7의 30개 계좌 및 거절 결과는 그대로 보존했다. 가격·거래량 가설의 기존 실패를 이 사건 조사로 뒤집지 않았다. 비용·갭·위험·자본 투입률을 포함하는 계좌 수준 검증은 새 규칙이 사전 등록될 때 별도 실험으로 필요하다.

181개 테스트, 48개 체인 증거 재검증, 328개 원문 해시 검증, 473,267개 아틀라스 행 재조회, 16개 가격 경로 독립 재계산이 통과했다. 원문부터 패널·대조군·결과까지 격리된 오프라인 재현의 8개 핵심 파일이 바이트 단위로 일치했다. 같은 작성자의 별도 프로세스 감사이며 외부 독립 감사나 체인 consensus/Merkle 증명은 아니다. [검증 상세](INDEPENDENT_REVIEW.md).

[사건 패널](EVENT_PANEL.md) · [아틀라스 연결](OPPORTUNITY_ATLAS.md) · [대조군](MATCHED_CONTROLS.md) · [프로토콜](PROTOCOL.md) · [정성 근거](QUALITATIVE_EVIDENCE.md) · [다음 결정](NEXT_DECISION.md)
''')
    direct_table='\n'.join('| '+e['event_id']+' | '+e['published_at']+' | '+e['first_seen_at']+' | '+(e['executed_at'] or '미입증 / null')+' | '+e['mechanism']+' |' for e in events if e['direct_value_accrual'])
    md('EVENT_PANEL.md',f'''# 사건 패널과 원문 증거

**정확한 원문 버전의 체인 공개**와 **이번 수집기의 수신**을 구분한다. 과거의 오프체인 최초 발표·수신이 입증됐다는 뜻이 아니다.

| 사건 | 체인 공개 (UTC) | 이 수집기의 최초 영속 수신 (UTC) | 경제적 실행 (UTC) | 메커니즘 |
|---|---|---|---|---|
{direct_table}

16개 후보 전체는 [event_panel.csv](event_panel.csv), 원문 SHA256·블록·트랜잭션·log index·영수증 해시는 [event_panel.json](event_panel.json)에 있다. 해시 없는 임의 요약을 피처로 사용하지 않는다. `historical_collector_first_seen_at=null`은 의도한 결측이다. 정성 자료도 수집 SHA256은 갖지만, 그 해시가 역사적 공개 시각을 증명하지는 않는다.

UNI는 세 governor의 모든 ProposalCreated와 queue/execute/cancel 로그를 열거했다. 원문 ABI를 직접 복호화한 뒤 14개 키워드 후보를 검토하여 7개 직접 정책과 7개 단순 언급을 구분했다. Foundation 지출의 'burn rate', 미래 fee switch를 언급한 법인 설립·거버넌스 문턱 변경은 직접 사건이 아니다. 취소·미실행 제안을 후보 목록에서 삭제하지 않았다. 한 비후보 Alpha 원문에 잘못된 UTF-8이 있어 원시 바이트 해시를 유지하고 대체 문자 표시는 따로 기록했다.

Aave GovernanceV3 생성 537개·실행 507개·취소 26개와 mainnet payload 생성 478개·실행 428개를 수집했다. 이는 2026-10-25 수집 시점의 이력이며 2026-10 이후 정보는 과거 selector에 사용하지 않는다. 후보 범위는 동결된 파일명 조건으로 제한된다. 실행 뒤 삭제된 소스는 공식 diff 도입 커밋에서 복원했다. CID 원문을 해시 검증한 #286만 직접 매입 allowance 사건으로 연결됐다. generic funding update, Snapshot-only 정책, 파일명에 검색어가 없는 정책을 포괄하지 못하므로 Aave 사건 전수조사가 아니다. [coverage.json](coverage.json)과 [UNI 전수 제안 목록](uni_proposal_inventory.json)을 함께 읽어야 한다.

## 증거 수준

Blockscout 블록/거래/원문 로그와 Routescan JSON 영수증의 주소·topics·data·log index·블록 해시·블록 번호·성공 상태·시각을 일치시켰다. Aave IPFS CID는 평문 SHA256으로 오인하지 않고 단일 UnixFS DAG-PB 노드의 multihash를 재계산했다. ETH는 London 직전/활성화 블록의 부모 해시 연결과 base fee 변화를 확인했다. 두 탐색기의 일치는 원본 일관성 증거이며 자체 Ethereum 노드 검증·영수증 Merkle proof·역사적 네트워크 최초 수신 증명은 아니다.

Aave의 onchain ProposalExecuted는 dispatch일 수 있으므로 실제 경제 설정은 PayloadExecuted로 추적했다. UNI 목적지 체인 실행을 수집하지 않은 경우에는 L1 실행을 경제적 완료 시각으로 쓰지 않았다. ETH는 실행 시점 사건만 채택했으며 EIP 작성일이나 London 수정 공지를 이전 시점 피처로 승격하지 않았다.

원문 바이트 328개(약 19.4MB), 요청/수신 영수증은 로컬 증거 묶음에 보관한다. Git에는 검토한 파생 패널과 원문 위치·해시만 제공한다. [source_catalog.json](source_catalog.json)은 해당 원문을 식별하며 원시 가격이나 HTTP 응답 전체를 포함하지 않는다.
''')
    md('OPPORTUNITY_ATLAS.md','''# 기존 아틀라스 연결

PR #7의 아틀라스 473,267행을 수정 없이 읽었다. 원본 gzip SHA256은 `2fad9446a5b10955b16bf8106ea85e3fc57bd2c2da2a6fd9eb7c2eee04001bb7`이다. 756개 시장·844,092개 일봉의 기존 보존/종료 시장 처리·연속 240일·거래대금 적격 기준을 그대로 따른다. 현재 생존 정보로 시장을 다시 고르지 않았다.

체인 공개+1시간이 들어 있는 UTC 날짜를 D로 삼고 D 완료 후 D+2 시가보다 빠른 가상 진입을 허용하지 않는다. 180일 D+2 라벨이 전체 및 각 구간 경계 안에서 완성돼야 한다. 사건별 최초 시그널 후 181일 동안 중복을 억제하며, 체결 성공·미래 수익·실행 여부로 다음 사건을 당기지 않는다.

16개 후보 → 직접 정책 9개 → 중복 5개·미성숙 1개 제외 → 3개 연결. 검증 구간 0건은 그대로 드러낸다. 실제 collector 시각을 적용한 연결은 0건이다. [scheduled_events.json](scheduled_events.json)과 [collector_schedule.json](collector_schedule.json)에 제외 사유를 남겼다.

90/180일 2/3배·도달시간·MAE·손절 선행·종료 상태를 그대로 연결했다. 미래값은 이 결과 라벨에만 존재한다. 사건 선택·중복 제거·대조군 검색에는 실행 완료 여부나 미래 수익을 넣지 않았다. 원본 아틀라스와 가격 데이터가 없는 공개 체크아웃만으로 시장 결과를 재현할 수는 없다. 데이터/원문 묶음을 별도로 공급하면 제공된 오프라인 스크립트로 재현할 수 있다.
''')
    matches='\n'.join('| '+p['event_id']+' | '+', '.join(c['symbol'] for c in p['controls'])+' | '+', '.join(c['symbol'] for c in p['failure_only_controls'])+' |' for p in pairs)
    md('MATCHED_CONTROLS.md',f'''# 동일 날짜 대조군

| 사건 | 결과를 보지 않고 선택한 대조군 | 별도 실패 전용 기술 대조군 |
|---|---|---|
{matches}

동일 D, 동일 과거 30일 유동성 bin·연속 거래이력 bin에서 로그 유동성·이력 거리의 합으로 가까운 3개를 선택했다. 동률은 symbol 순이다. 사건 토큰과 D에 이미 알려진 패널의 181일 노출 토큰을 제외했다. 결과와 실행 여부는 선택에 쓰지 않는다. 3건 모두 3개 대조군이 있어 9개, 같은 종목-날짜 재사용 0개다. 같은 토큰의 다른 날짜나 시장 동조화가 독립성을 보장하지는 않는다.

추가 실패 전용 표는 180일 2배 실패에 조건부인 **사후 기술 비교**다. 이 표에서 성공률·기본확률을 산출하지 않았다. 9개 일반 대조군 중 성공 2개를 삭제하지 않았다. 과거 PR #7의 hindsight 매칭 결과를 재활용하여 확률로 오인하지 않았다.

[matched_controls.csv](matched_controls.csv)에 역할·신호일·거래이력·유동성 bin·국면·거래대금 변화·BTC 상대성과·변동성 변화·라벨을 제공한다. 경제적으로 다른 토큰을 거래 규모와 이력만으로 비교해 생기는 교란은 남는다. 실거래 가격전용 전략과의 계좌 성과 차이가 아니라 사건 후 목표도달률의 기술 비교다. 사건별 원문은 [event_panel.json](event_panel.json), 전체 확률/구간은 [statistics.json](statistics.json)에 있다.
''')
    md('PROTOCOL.md','''# 사전 동결과 변경 경계

연구 규칙은 가격 연결 전에 원격 커밋 `06d73b1a7e1e390ef54a75f596d4edb62cb6f3c9`로 게시했다. 원문 분류·패널·코드·181개 테스트 확인 후 `5951fc60df487c939c30c508b1e4536d3a4365cb`로 코드/패널 해시를 원격 동결한 다음 새 사건 가격 결과를 조회했다. 두 원격 커밋의 Git tree가 해당 로컬 동결 커밋과 일치함을 확인했다.

[전체 프로토콜](../../VALUE_ACCRUAL_PROTOCOL.json) · [원문 분류](../../VALUE_ACCRUAL_CLASSIFICATIONS.json) · [코드·패널 동결 해시](../../VALUE_ACCRUAL_FREEZE.json).

개발 2019–2022, 검증 2023–2024, 도전 2025–2026-09를 유지한다. 선행 공개+1시간, D+2, 182일 episode 간격, 180일 라벨 완성, exact-date·liquidity-bin·age-bin 매칭, seed 20261025와 2,000회 기술 bootstrap, 최소 검증/도전 각각 30사건·10토큰 군집을 고정했다. 모든 파라미터의 실증 탐색 횟수는 0이다. 원문 의미 분류는 새 가격 결과 조회 전에 완료했다. 이전 아틀라스 전체 결과와 세 토큰의 유명 사례는 이미 알고 있었다.

집행 규칙이나 계좌 수익성 검증을 새로 만든 실험이 아니다. 과거 PR #7의 가격·거래량 계좌 검증과 REJECT 판정은 유지한다. 이번의 수익 라벨을 보고 보유기간·손절·진입 시계·후보 토큰을 바꾸지 않았다. 특히 Aave의 실행 후 경로가 유리하다는 이유로 발표 기준의 주 분석을 실행 기준으로 교체하지 않았다.

수집/구현 중 처리한 문제: 실제 Alpha 문자열의 invalid UTF-8 원시 바이트를 보존하고 표시만 대체 문자 처리; 삭제된 Aave 소스를 diff 도입 커밋으로 복원; 최초 IPFS 게이트웨이 429 이후 다른 무료 게이트웨이에서 CID를 검증; 캐시 인덱스보다 append-only 실제 영수증을 우선하여 최초 수신시각 복원. 모두 새 가격 결과 연결 전에 반영했다. 근거가 불명확한 원문은 정성으로 분리했다. 기존 연구 파일은 변경하지 않았다.
''')
    md('QUALITATIVE_EVIDENCE.md','''# 과거 빈티지가 입증되지 않은 근거

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
''')
    md('INDEPENDENT_REVIEW.md',f'''# 독립 계산과 재현 — PASS

같은 작성자가 별도 프로세스와 대체 계산으로 검증했다. 외부 감사 의견이 아니다.

- 원문 HTTP 성공 영수증 **{review['raw_successful_receipts']}개**, 고유 바이트 SHA256 **{review['raw_unique_content_hashes']}개** 재계산.
- 체인 공개/queue/cancel/dispatch/payload/실행 증거 **{review['chain_proofs_rechecked']}개**를 raw 응답에서 독립 대조.
- 원본 UNI 문자열의 ABI offset·길이·SHA256, Aave CID의 UnixFS DAG-PB preimage·온체인 커밋 대조.
- 과거 수신시각을 만들지 않았는지 append-only 수신 영수증과 검사.
- 아틀라스 **473,267행**을 다시 읽어 정확한 키로 연결하고 사건 3건의 일반/실패 대조군을 독립 재선택.
- 시장 DB의 **756시장·844,092일봉**에서 연결된 고유 가격 경로 **16개**의 research/D+2 라벨과 당시 피처를 PR #7의 대체 구현으로 재계산.
- 오프라인 격리 복제에서 원문 목록→IPFS→체인 증거→패널→연결을 처음부터 재실행. 핵심 8개 파일의 바이트가 일치하고 시간만 다른 manifest는 비교에서 명시적으로 제외.
- 181개 단위/회귀 테스트, compileall, CLI help, status(`NOT_STARTED`) 통과. 원본 HEAD 대비 변경은 추가 파일만 존재.

[기계 판독 감사](independent_review.json) · [오프라인 재현](reproduction.json) · [결과 SHA256](PUBLIC_ARTIFACT_HASHES.json).

새 전략이나 거래 원장을 만들지 않아 새 계좌 성과를 검증했다는 주장은 없다. 원장 관련 기존 PR #7의 30개 계좌·독립 원장검증·REJECT 판정은 그대로 남아 있다. 본 연구의 신호/라벨 무결성 통과는 비용·자본 활용·실거래 수익성 통과가 아니다. 새 파일을 추가한 체크아웃에서 예전 전체-glob 코드 freeze를 갱신하지 않았으며, PR #7 계좌를 그대로 재현하려면 해당 HEAD의 별도 체크아웃을 사용한다.
''')
    md('NEXT_DECISION.md','''# 다음 결정

**NEEDS_MORE_EVIDENCE. 검증된 우위 없음. 실거래 적격 없음.**

다음 단 하나의 행동은 **사전 등록한 10개 이상 토큰의 전향 사건 패널 수집**이다. 실제 수신 영수증과 원문 버전 해시를 남기고, 제안·거절·취소·중단·희석·실행을 모두 보존한 뒤 180일 성숙을 기다린다. 최신 대시보드나 현재 문서의 과거 날짜로 수신 시간을 채우지 않는다. 이 PR은 그 작업을 예약하지 않았다.

이번 결과의 제한: 예측력은 검증 구간 0건·도전 2건으로 확인 불가; 오탐은 주 사건 3개 중 2개; 유일한 2배 성공은 발표 진입에서 손절이 선행; 발표/실행의 시계 차이가 경로를 크게 바꿈; 비용·자본 활용·위험 제한을 가진 새 전략은 미설계. 따라서 세 사례의 목표도달률을 월 수익 목표나 자본 효율의 증거로 바꾸지 않는다. 레버리지·사후 진입 변경으로 부족한 증거를 보완하지 않는다.

전향 수집 후에만 새로운 매매 가설을 최대 2개로 다시 사전 등록하고, 이전과 같은 D+2·기본/스트레스 비용·갭·종료시장·위험/자본 제약·계좌 원장 검증을 거쳐 판단한다. 기존 거절 결과는 별개로 보존한다.
''')
    md('README.md','''# 가치귀속 사건 패널

**검증된 우위 없음 / 선행 공개 3건의 매매 가능 우위 미입증 / 다음 행동은 전향 시각 증거 수집.**

먼저 [RESULTS](RESULTS.md)를 읽는다. 이어 [EVENT_PANEL](EVENT_PANEL.md), [OPPORTUNITY_ATLAS](OPPORTUNITY_ATLAS.md), [MATCHED_CONTROLS](MATCHED_CONTROLS.md), [PROTOCOL](PROTOCOL.md), [INDEPENDENT_REVIEW](INDEPENDENT_REVIEW.md), [NEXT_DECISION](NEXT_DECISION.md)를 제공한다. 재현 명령은 [워크플로](../../VALUE_ACCRUAL.md)에 있다.
''')
    save_json(out/'PUBLIC_ARTIFACT_HASHES.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file() and p.name!='PUBLIC_ARTIFACT_HASHES.json'})
    print(json.dumps(dict(report_files=len(list(out.iterdir())),sources=len(sources),path=str(out))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('evidence','study','reproduction'):p.add_argument('--'+flag,type=Path,required=True)
    a=p.parse_args();export(a.evidence,a.study,a.reproduction)
