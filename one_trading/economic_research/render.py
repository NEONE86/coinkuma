"""Deterministic human-readable report from the stored analysis, no model call."""
from decimal import Decimal


def number(value, places=4):
    if value is None:
        return "미측정"
    return f"{Decimal(value):,.{places}f}"


def pct(value):
    return number(Decimal(value) * 100, 2) + "%"


def markdown(report):
    counts = report["scoring"]["counts"]
    lines = ["# ONE Trading — Repricing Research v4", "",
             f"판단 보고 기준 UTC: {report['as_of']}", "",
             "현재 판단은 아래 저장된 증거와 주관적 가정을 연결한 연구 기록이다. 투자 수익성은 미검증이며 행동 상태는 각 카드에 표시한다.", "",
             "| 자산 | 연구 선택 | 현재 행동 | 고정 시한까지 예상 순수익 | 최악 시나리오 | 요구수익 진입가격 |",
             "|---|---|---|---:|---:|---:|"]
    for card in report["cards"]:
        d, t = card["decision"], card["thesis"]
        v = d["valuation"]
        symbol = t["thesis_id"].split("-")[0]
        lines.append(f"| {symbol} | {d['research_choice']} | {d['investment_action']} | {pct(v['expected_return'])} | {pct(v['worst_scenario_return'])} | {number(v['entry_for_required_return'])} |")
    lines += ["", "예상수익은 실측 수익이 아니다. 단위 토큰의 매수·기말매도 비용을 적용한 시나리오 산술이며 계좌 수익률·포지션 손익과 구분한다.", "",
              f"전체 등록 {counts['registered_forecasts']}개, 평가 대상으로 선택 {counts['selected_forecasts']}개, 기한 도래 {counts['due_forecasts']}개, 해결 {counts['resolved_forecasts']}개, 미해결 {counts['unresolved_forecasts']}개, 독립 사건으로 묶은 군집 {counts['independent_event_clusters']}개.", "",
              "점수 분모는 실제 해결된 예측뿐이다. 군집 수는 통계적 독립성을 입증하지 않는다. 해결된 사례만의 점수에는 자료 누락 편향이 남는다."]
    lines += ["", f"공통 질문 {counts['selected_questions']}개, 해결된 공통 질문 {counts['resolved_questions']}개. 정책별 예측 평가는 같은 사건의 독립 표본 수를 늘리지 않는다."]
    legacy = report["scoring"].get("legacy_review", {})
    if legacy.get("quarantined_questions"):
        lines += ["", f"격리된 구버전 질문 {legacy['quarantined_questions']}개 / 예측 {legacy['quarantined_forecasts']}개. 원본 감사 문제는 유지하며 점수와 학습에서 제외한다."]
        for row in legacy["quarantines"]:
            lines.append(f"- `{row['question_id']}`: {row['reason']} — {row['review_reason']}")
        for link in legacy["recovery_links"]:
            lines.append(f"- 복구 연결 `{link['old_question_id']}` → `{link['new_question_id']}`: {link['review_reason']}")
    if report["scoring"]["scores"]:
        lines += ["", "| 정책 | 대상 | 점수 | 단위 | 평가 수 | 오차 |", "|---|---|---|---|---:|---:|"]
        for s in report["scoring"]["scores"]:
            lines.append(f"| {s['policy_id']} | {s['target_type']} | {s['score_name']} | {s['unit']} | {s['score_denominator']} | {number(s['mean_loss'])} |")
    for comparison in report["candidate_comparisons"]:
        lines += ["", "## 후보 비교", "", *["- " + reason for reason in comparison["pairwise_reasons"]], "",
                  "다음 단 하나의 행동: " + comparison["next_single_action"]]
    for card in report["cards"]:
        d, t, packet = card["decision"], card["thesis"], card["packet"]
        v = d["valuation"]
        symbol = t["thesis_id"].split("-")[0]
        lines += ["", "## " + symbol, "", d["reason"], "", "경제 가설: " + t["primary_explanation"], "",
                  "시장 기대와의 차이: " + t["market_expectation_gap"], "",
                  "판정을 바꾸는 조건: " + d["change_conditions"], "",
                  "다음 조사 질문: " + t["next_high_value_question"], "",
                  f"등록 UTC: {d['decision_at']}; 입력 마감: {packet['as_of']}; 평가 시한: {d['horizon']}; 가격 상태: {d['price_status']}; 행동 상태: {d['action_execution_status']}."]
        price = card.get("price_snapshot")
        if price:
            lines += ["", f"관측 가격: {number(price['value'])} {price['quote_currency']} / {price['venue']} {price['instrument']} {price['price_kind']}; 거래소 시각 {price['source_time']}; 실제 수신 {price['observed_at']}."]
        lines += ["", "### 근거의 성격", ""]
        for o in card["observations"]:
            value = "미측정 — " + o.get("missing_reason", "") if o["value"] is None else str(o["value"])
            lines.append(f"- **{o['epistemic_type']}** `{o['observation_id']}`: {value}")
        lines += ["", "### 경쟁 설명과 반증", ""]
        for rival in t["competing_explanations"]:
            lines.append("- " + rival["explanation"] + " 관측 구분: " + rival["distinguishing_observation"])
        lines += ["- 반증: " + f for f in t["falsifiers"]]
        lines += ["", "### 기한이 있는 예측", "", "| 유형 | 기한 UTC | 예측 | 확률/분포 | 상태 |", "|---|---|---|---|---|"]
        for f in card["forecasts"]:
            dist = f["probability_or_distribution"]
            estimate = pct(dist["p"]) if dist["type"] == "BINARY" else "; ".join(number(p["value"]) + " @ " + pct(p["p"]) for p in dist["points"])
            lines.append(f"| {f['target_type']} | {f['deadline']} | {f['target_definition']} | {estimate} | {f['status']} |")
        lines += ["", "확률은 JUDGMENT이며 미상 기본 발생률은 null이다. 사건·경제효과 확률을 독립으로 곱하지 않았다. 정확한 측정창·허용 출처·해결 규칙은 JSON 보고서의 resolution_rule에 동결되어 있다.", "",
                  "### 같은 시한의 가격 시나리오", "", "| 시나리오 | 결합 확률 | 기말 가격 | 기말 경제적 공급 가정 | 단위 현금분배 | 경제 가정 |", "|---|---:|---:|---:|---:|---|"]
        inputs = {s["id"]: s for s in d["scenario_tree"]["scenarios"]}
        for valued in v["scenarios"]:
            scenario = inputs[valued["id"]]
            lines.append(f"| {scenario['id']} | {pct(valued['joint_probability'])} | {number(scenario['terminal_price'])} | {number(scenario['terminal_token_supply'], 0)} | {number(scenario['cash_distribution'])} | {scenario['economic_assumptions']} |")
        lines += ["", f"결합 확률 합계: {pct(sum(Decimal(s['joint_probability']) for s in v['scenarios']))}. 현금분배는 위 시나리오별 가정이다.", "",
                  f"비용: 매수 {pct(d['cost_assumptions']['buy_fraction'])}, 매도 {pct(d['cost_assumptions']['sell_fraction'])}. 손익분기 진입가격 {number(v['break_even_entry'])}; 요구수익 {pct(v['required_return'])}의 진입가격 {number(v['entry_for_required_return'])}.", "",
                  "가격 민감도: " + "; ".join(number(r["entry_price"]) + " → " + pct(r["expected_return"]) for r in v["price_sensitivity"]), "",
                  "### 버전과 출처", "", f"Packet `{packet['packet_id']}`; manifest `{packet['manifest_hash']}`.", "",
                  f"원인 모형 `{t['causal_model_version']}`, 추정 `{d['estimation_version']}`, 정책 `{d['policy_version']}`.", ""]
        for source in card["sources"]:
            lines.append(f"- [{source['source_url']}]({source['source_url']}) — SHA256 `{source['sha256']}`, 실제 최초 수신 {source['first_observed_at']}.")
        if card.get("current_sensitivity_diagnostics"):
            diagnostic = card["current_sensitivity_diagnostics"]
            lines += ["", "추가 가정 민감도(원래 판단을 수정하지 않은 재계산): 최선→최악 결합확률 10%p 이전 시 기대수익 "
                      + pct(diagnostic["probability_sensitivity"]["expected_return"]) + "; 편도 비용 1% 이상 시 "
                      + pct(diagnostic["cost_sensitivity"]["expected_return"]) + "."]
    lines += ["", "## 실행 범위와 미검증", "", "- 원문·가격·분석 원장은 Git 밖에 보존한다. 보고서는 저장 기록으로 재생성한다.",
              "- 이유 작성은 Codex의 manual_import이다. Python에서 무인 모델 추론을 실행하지 않았고 모델 API 호출 상태는 NOT_RUN이다.",
              "- 실제 주문·계좌 연결·상시 배포·모의 체결을 수행하지 않았다. D+2·기존 브로커·손절·갭·계좌 원장은 이 단위 가격 예측에 연결하지 않았다.",
              "- 실현 경제효과, 확률 보정, 실현 가격 결과, 거래비용 및 위험 한도가 있는 정책 수익성은 아직 검증되지 않았다.",
              "- 합성 만기·정책 비교 테스트는 소프트웨어 경로 검증이며 실전 학습이나 수익성 검증이 아니다.",
              f"- 실제 HTTP 영수증 {report['source_coverage']['attempts']}건, 성공 {report['source_coverage']['successes']}건, 실패 {len(report['source_coverage']['failures'])}건. 성공은 요청 범위의 응답 수신일 뿐 사건·체인 전수조사를 뜻하지 않는다.", ""]
    return "\n".join(lines)
