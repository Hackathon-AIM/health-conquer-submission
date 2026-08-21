"""L2 · 의도 분류 (LLM 1회, 1~2초)

담당: D

핵심 두 가지
  1. theme이 아니라 intent를 뽑는다
     "어제 술 먹고 머리 아파요" → theme=symptom 이지만 답변에는 진통제가 나온다.
     그래서 expects_drug_output 을 함께 예측해 L4b 게이트를 미리 켠다.

  2. 결론보다 이유를 먼저 생성시킨다
     LLM은 왼쪽→오른쪽으로 생성하므로 JSON 필드 순서가 곧 사고 순서다.
       {"need_followup": true, "justification": "..."}  ← 사후 합리화. 효과 없음
       {"why_it_changes_answer": "...", "need_followup": true}  ← 실제 판단
     "되물을까?"만 물으면 모델은 거의 항상 true를 낸다(안전해 보이니까).
     "그 정보가 없으면 답이 어떻게 달라지는지 써라"를 강제하면 쓸 말이 없을 때
     스스로 false를 낸다. HealthBench에서 불필요한 되묻기는 감점이므로 이 제약이 곧 점수.
"""

from __future__ import annotations

from . import entities as ent
from .config import Config, prompt
from .contracts import Entities, Intent, QueryPlan, RedFlagHit, RiskFactor, SessionState
from .llm import LLM


def _fewshot_l2() -> list[dict[str, str]]:
    """l2_native 용 few-shot — 응답 설계 필드 포함, queries 제외.

    ★ 두 예시가 각각 다른 것을 가르친다:
      1) 단순 조회형 → depth=brief, 되묻지 않음   (장황함·불필요한 되묻기 방지)
      2) 맥락 부족형 → 답하면서 한 가지만 되물음  (티키타카의 본보기)
    """
    return [
        {"role": "user", "content": "타이레놀 하루에 최대 몇 알까지 먹어도 되나요?"},
        {"role": "assistant", "content": (
            '{"reasoning":"특정 제품의 1일 용량 상한을 묻는 단순 조회형 질문이다.",'
            '"missing_info":[],"why_it_changes_answer":"",'
            '"intent":"drug_safety","need_followup":false,"clarifying_question":"",'
            '"depth":"brief","uncertainty":"","persona":"layperson",'
            '"entities":{"drugs":[{"name":"타이레놀","span":"타이레놀"}],"symptoms":[],'
            '"risk_factors":[],"temporal":null},'
            '"expects_drug_output":true}'
        )},
        {"role": "user", "content": "어제 술을 너무 많이 마셨는데 머리가 너무 아파요"},
        {"role": "assistant", "content": (
            '{"reasoning":"음주 후 두통 상담. 약물명은 없지만 답변에 진통제가 등장할 가능성이 높다.",'
            '"missing_info":["평소 음주 빈도","간 질환 여부"],'
            '"why_it_changes_answer":"간 질환이나 상습 음주가 있으면 아세트아미노펜을 권할 수 없다.",'
            '"intent":"symptom_consult","need_followup":true,'
            '"clarifying_question":"평소에도 자주 드시는 편인지, 간 질환 진단을 받으신 적이 있는지 알려주시겠어요?",'
            '"depth":"standard","uncertainty":"","persona":"layperson",'
            '"entities":{"drugs":[],"symptoms":["두통"],'
            '"risk_factors":[{"type":"alcohol","when":"어제","detail":"과음"}],"temporal":"어제"},'
            '"expects_drug_output":true}'
        )},
    ]


def _fewshot() -> list[dict[str, str]]:
    """도메인 특화 모델은 지시 따르기가 약할 수 있다.

    Gravity 계열 공개 모델은 전부 Base(채팅 템플릿 없음)이므로,
    대회 임상 모델의 instruction-following은 검증된 바가 없다.
    few-shot이 가장 싸고 즉효인 방어책.
    """
    return [
        {"role": "user", "content": "타이레놀 하루에 최대 몇 알까지 먹어도 되나요?"},
        {"role": "assistant", "content": (
            '{"reasoning":"특정 제품의 용량 상한을 묻는 단순 조회형 질문이다.",'
            '"missing_info":[],"why_it_changes_answer":"",'
            '"intent":"drug_safety","need_followup":false,"persona":"layperson",'
            '"entities":{"drugs":[{"name":"타이레놀","span":"타이레놀"}],"symptoms":[],'
            '"risk_factors":[],"temporal":null},'
            '"extra_sources":[],"queries":{"drug":"타이레놀 1일 최대 용량"},'
            '"expects_drug_output":true}'
        )},
        {"role": "user", "content": "어제 술을 너무 많이 마셨는데 머리가 너무 아파요"},
        {"role": "assistant", "content": (
            '{"reasoning":"음주 후 두통 상담. 약물명은 없지만 답변에 진통제가 등장할 가능성이 높다.",'
            '"missing_info":["평소 음주 빈도","간 질환 여부"],'
            '"why_it_changes_answer":"상습 음주 여부에 따라 권할 수 있는 진통제 계열이 달라진다.",'
            '"intent":"symptom_consult","need_followup":true,"persona":"layperson",'
            '"entities":{"drugs":[],"symptoms":["두통"],'
            '"risk_factors":[{"type":"alcohol","when":"어제","detail":"과음"}],"temporal":"어제"},'
            '"extra_sources":[],"queries":{"guideline":"숙취 두통 관리","drug":"음주 후 진통제 주의사항"},'
            '"expects_drug_output":true}'
        )},
    ]


async def classify(
    text: str,
    session: SessionState,
    llm: LLM,
    cfg: Config,
    red_flag: RedFlagHit | None = None,
) -> QueryPlan:
    # ── L1 규칙 추출 (0ms) ─────────────────────────────────
    rule_ents = ent.extract(text, age=session.age)

    # 응급이면 LLM 호출조차 생략 — 지연 0으로 응급 경로 진입
    if red_flag is not None:
        return QueryPlan(
            reasoning=f"레드플래그 매칭: {red_flag.category} / {red_flag.matched}",
            intent=Intent.EMERGENCY,
            need_followup=False,
            entities=rule_ents,
            red_flag=red_flag,
            expects_drug_output=False,
        )

    known = ", ".join(session.known_keys()) or "없음"

    # l2_native 에서는 검색어(queries)를 우리가 만들지 않는다 — L2 가 검색 단계에서
    # 스스로 만든다. 그 필드를 빼고 대신 응답 설계 필드를 받는다.
    # 출력 토큰이 줄어드는 만큼 분류 지연도 줄어든다 (실측 33.7s 가 병목이었다).
    native = cfg["layers"].get("l2_native", False)
    sys = prompt("classifier_l2.txt" if native else "classifier.txt")
    sys = sys.replace("{{KNOWN}}", known)

    messages = (
        [{"role": "system", "content": sys}]
        + (_fewshot_l2() if native else _fewshot())
        + [{"role": "user", "content": text}]
    )
    raw = await llm.chat_json(
        "classifier", messages,
        max_tokens=int(cfg["llm"].get("classifier_max_tokens", 600)),
    )

    plan = _to_plan(raw, text, rule_ents, cfg)
    return plan


def _to_plan(raw: dict, text: str, rule_ents: Entities, cfg: Config) -> QueryPlan:
    """LLM 출력 → QueryPlan. 파싱 실패해도 규칙 결과로 계속 진행한다."""
    try:
        intent = Intent(raw.get("intent", "symptom_consult"))
    except ValueError:
        intent = Intent.SYMPTOM_CONSULT

    e = raw.get("entities") or {}

    # ⭐ 규칙 ∪ LLM 합집합 → span 검증 → 사전 필터
    if cfg["layers"].get("entity_llm_merge", True):
        drugs = ent.merge_drug_candidates(
            text,
            rule_hits={d.name for d in rule_ents.drugs},
            llm_mentions=e.get("drugs") or [],
        )
    else:
        drugs = rule_ents.drugs

    # 위험인자: 규칙 결과를 기준으로 하되 LLM이 찾은 타입을 보충
    risks = list(rule_ents.risk_factors)
    seen = {r.type for r in risks}
    for r in e.get("risk_factors") or []:
        t = r.get("type")
        if t and t not in seen:
            try:
                risks.append(RiskFactor(**r))
                seen.add(t)
            except Exception:
                pass

    plan = QueryPlan(
        reasoning=str(raw.get("reasoning", ""))[:400],
        missing_info=[str(x) for x in (raw.get("missing_info") or [])][:4],
        why_it_changes_answer=str(raw.get("why_it_changes_answer", ""))[:300],
        intent=intent,
        need_followup=bool(raw.get("need_followup", False)),
        depth=(raw.get("depth") if raw.get("depth") in ("brief", "standard", "thorough")
               else "standard"),
        clarifying_question=str(raw.get("clarifying_question", ""))[:200],
        uncertainty=str(raw.get("uncertainty", ""))[:300],
        persona=raw.get("persona") if raw.get("persona") in ("layperson", "clinician") else "layperson",
        entities=Entities(
            drugs=drugs,
            symptoms=[str(s) for s in (e.get("symptoms") or [])][:6],
            risk_factors=risks,
            temporal=e.get("temporal") or rule_ents.temporal,
        ),
        extra_sources=[str(s) for s in (raw.get("extra_sources") or [])],
        queries={str(k): str(v) for k, v in (raw.get("queries") or {}).items()},
        expects_drug_output=bool(raw.get("expects_drug_output", False)),
    )

    # 되묻기 게이트: 이유가 비어 있으면 되묻지 않는다.
    # (모델이 근거 없이 true를 낸 경우를 코드로 한 번 더 막는다)
    if plan.need_followup and not plan.why_it_changes_answer.strip():
        plan.need_followup = False
    # 되묻기와 실제 질문 문장은 하나의 상태여야 한다 — 한쪽만 있으면 무효
    if not plan.need_followup:
        plan.clarifying_question = ""
    elif not plan.clarifying_question.strip():
        plan.need_followup = False

    # 약물이 언급됐으면 출력에도 나올 가능성이 높다
    if plan.entities.drugs:
        plan.expects_drug_output = True

    # 검색어가 비었으면 원문으로 폴백
    if not plan.queries:
        plan.queries = {"guideline": text[:120]}

    return plan
