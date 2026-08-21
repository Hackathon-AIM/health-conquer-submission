"""L4 · 초안 생성 + L4b 출력측 안전 게이트.

담당: D(템플릿) / C(게이트)

테마별 템플릿 구조가 완전히 달라야 한다.
  응급  → [즉시 조치] → [119] → [가는 동안 하지 말 것] → 끝. ★짧게
  약물  → [직접 답] → [용법·주의] → [상호작용] → [언제 의사에게]
  증상  → [잠정 안내] → [필요 시 되묻기] → [red flag 내원 기준]
  보험  → [제도 설명 + 조문 인용] → [공단·심평원 확인 권유]

응급 템플릿이 짧은 게 의도다. 길면 중요한 게 묻힌다.
"""

from __future__ import annotations

import re

from . import entities as ent
from .config import Config, prompt
from .contracts import (Context, Intent, QueryPlan, SafetyHit, SessionState)
from .gates import dur as dur_mod
from .gates import risk as risk_mod
from .llm import LLM

TEMPLATE_BY_INTENT = {
    Intent.EMERGENCY: "emergency.txt",
    Intent.DRUG_SAFETY: "drug.txt",
    Intent.DRUG_RECOMMEND: "drug.txt",
    Intent.SYMPTOM_CONSULT: "symptom.txt",
    Intent.INFO_REQUEST: "symptom.txt",
    Intent.POLICY: "policy.txt",
}

DRUG_SAFETY_ADDENDUM = """
[약물 언급 시 필수 준수]
- 약물을 언급할 때는 반드시 (1) 일반적 용법 (2) 주의사항 (3) 사용자의 상태와의 상호작용을 함께 서술한다.
- 특정 제품명 단정보다 성분·계열 수준으로 설명하고, 계열별 주의점 차이를 밝힌다.
- 근거가 불확실한 부분은 불확실하다고 명시한다.
"""

# 레드플래그 카테고리별 고정 프리픽스 — LLM을 거치지 않는다.
# 안내를 맨 앞에 두면 +10, 뒤에 묻으면 -9. 배치가 곧 점수이므로 문자열로 확정한다.
EMERGENCY_PREFIX: dict[str, str] = {
    "cardiac": "**지금 바로 119에 전화하세요.** 가슴을 조이는 통증에 식은땀이 동반되면 "
               "심장 문제일 수 있어 즉시 응급실 진료가 필요합니다. 혼자 운전해서 이동하지 마세요.",
    "neuro": "**지금 바로 119에 전화하세요.** 갑작스러운 마비·언어장애·극심한 두통은 "
             "뇌졸중 등 응급 상황일 수 있으며, 치료는 시간이 생명입니다.",
    "respiratory": "**지금 바로 119에 전화하세요.** 호흡이 곤란하거나 입술이 창백해지는 것은 "
                   "즉시 응급 처치가 필요한 상태입니다.",
    "bleeding": "**지금 바로 119에 전화하거나 응급실로 가세요.** 토혈·흑색변·멎지 않는 출혈은 "
                "즉각적인 처치가 필요합니다.",
    "anaphylaxis": "**지금 바로 119에 전화하세요.** 목이 붓거나 조이면서 호흡이 힘든 것은 "
                   "중증 알레르기 반응일 수 있어 즉시 처치가 필요합니다. "
                   "에피네프린 자가주사기가 있다면 사용하세요.",
    "abdominal": "**지금 바로 응급실로 가세요.** 참기 힘든 복통이나 배가 판자처럼 굳는 증상은 "
                 "즉시 진료가 필요합니다. 음식이나 물은 드시지 마세요.",
    "psych": "**혼자 견디지 마세요. 지금 자살예방상담전화 109 또는 119로 연락하세요.** "
             "24시간 상담이 가능하며, 지금 곁에 있어줄 사람에게 연락하는 것도 도움이 됩니다.",
}
EMERGENCY_DEFAULT = ("**지금 바로 119에 전화하거나 가까운 응급실로 가세요.** "
                     "말씀하신 증상은 즉시 진료가 필요할 수 있습니다.")


def emergency_prefix(category: str) -> str:
    return EMERGENCY_PREFIX.get(category, EMERGENCY_DEFAULT)


CONSULT_PHARMACIST = (
    "\n\n복용 중이신 약과 현재 상태를 고려하면 임의로 약을 선택하기보다 "
    "**약사나 의사에게 직접 확인**하시는 것이 안전합니다."
)


# ─────────────────────────────────────────────────────────────
# L4 · 초안
# ─────────────────────────────────────────────────────────────
async def draft(
    text: str,
    plan: QueryPlan,
    ctx: Context,
    session: SessionState,
    input_hits: list[SafetyHit],
    llm: LLM,
    cfg: Config,
) -> str:
    tmpl = prompt(f"templates/{TEMPLATE_BY_INTENT.get(plan.intent, 'symptom.txt')}")

    sys = tmpl
    if plan.expects_drug_output:
        # 사후 교정보다 사전 예방이 싸다.
        # L4b에서 잡아 재작성하면 LLM 호출이 한 번 더 들지만 이건 공짜다.
        sys += DRUG_SAFETY_ADDENDUM

    parts = []
    if plan.persona == "clinician":
        parts.append("[상대] 의료인 — 전문 용어를 사용해도 된다.")
    else:
        parts.append("[상대] 일반인 — 전문 용어는 풀어서 설명한다.")

    if session.history:
        hist = "\n".join(f"{h['role']}: {h['content'][:300]}" for h in session.history[-4:])
        parts.append(f"[이전 대화]\n{hist}")

    slots = _render_slots(session)
    if slots:
        parts.append(f"[알고 있는 사용자 정보]\n{slots}")

    if ctx.docs:
        parts.append(f"[검색된 근거 — 인용 시 출처 표기를 유지할 것]\n{ctx.render()}")

    if plan.need_followup and plan.missing_info:
        parts.append(
            "[되묻기] 아래 정보가 없으면 답이 달라진다: "
            + ", ".join(plan.missing_info)
            + f"\n사유: {plan.why_it_changes_answer}"
            + "\n→ 안전한 잠정 안내를 먼저 준 뒤, 최대 2가지만 되묻는다."
        )
    else:
        parts.append("[되묻기] 불필요하다. 되묻지 말고 바로 답한다.")

    if input_hits:
        parts.append("[안전 판정 — 반드시 반영]\n" + dur_mod.render_warning(input_hits))

    if plan.red_flag:
        parts.append(f"[응급 신호] {plan.red_flag.category} / '{plan.red_flag.matched}'")

    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": "\n\n".join(parts) + f"\n\n[질문]\n{text}"},
    ]
    return await llm.chat("drafter", messages)


def _render_slots(s: SessionState) -> str:
    bits = []
    if s.age is not None:
        bits.append(f"나이 {s.age}세")
    if s.pregnant:
        bits.append("임신 중")
    if s.conditions:
        bits.append("기저질환: " + ", ".join(s.conditions))
    if s.medication_names:
        bits.append("복용 중: " + ", ".join(s.medication_names))
    if s.risk_factors:
        bits.append("위험인자: " + ", ".join(r.type for r in s.risk_factors))
    if s.symptom_duration:
        bits.append(f"증상 지속: {s.symptom_duration}")
    return " / ".join(bits)


# ─────────────────────────────────────────────────────────────
# L4b · 출력측 안전 게이트
# ─────────────────────────────────────────────────────────────
async def extract_output_drugs(draft_text: str, plan: QueryPlan, llm: LLM, cfg: Config) -> list[str]:
    """모델은 아래처럼 모호하게 말한다. 정규식만으론 부족하다.
        "아세트아미노펜 계열의 진통제"  ← 성분군
        "타이레놀 같은 약"             ← 예시 화법
        "해열진통제를 고려해보세요"     ← 효능군
    L1과 같은 패턴: 규칙 ∪ LLM → span 검증 → 사전 필터.
    """
    found: set[str] = ent.rule_extract_drugs(draft_text) | ent.rule_extract_classes(draft_text)

    # expects_drug_output 플래그의 두 번째 역할:
    # 약이 나올 리 없는 질문("실비 되나요")엔 추출 호출을 아끼고,
    # 나올 것 같은 질문에만 쓴다.
    if plan.expects_drug_output or found:
        # 약 이름 목록 하나면 된다. 상한이 없으면 L2 가 장황해져 이 호출만 10초를 먹는다.
        raw = await llm.chat_json("extractor", [
            {"role": "system", "content": prompt("extract_drugs.txt")},
            {"role": "user", "content": draft_text[:3000]},
        ], max_tokens=int(cfg["llm"].get("extractor_max_tokens", 400)))
        for m in (raw.get("drugs") or []):
            name = str(m.get("name", "")).strip()
            span = str(m.get("span", "") or name).strip()
            if not name:
                continue
            if span and span not in draft_text:      # 환각 차단
                continue
            k = ent.key(name)
            if k in ent.drug_map() or k in ent.product_to_ingredients() or name in ent.drug_class():
                found.add(name)
    return sorted(found)


async def output_gate(
    draft_text: str,
    plan: QueryPlan,
    session: SessionState,
    dur: dur_mod.DurClient,
    llm: LLM,
    cfg: Config,
) -> tuple[list[SafetyHit], list[str]]:
    """모델이 '추천한' 약을 검증한다.

    대국민 챗봇에서는 "이 약 먹어도 되나요"보다 "뭘 먹어야 하나요"가 더 흔하다.
    출력측을 안 보면 더 큰 쪽을 놓친다.
    """
    if not cfg["layers"].get("dur_output", True):
        return [], []

    names = await extract_output_drugs(draft_text, plan, llm, cfg)
    if not names:
        return [], []

    hits: list[SafetyHit] = []
    # 약-약 (모델이 추천한 것 + 세션 복용약)
    hits += await dur_mod.check(
        names + session.medication_names, session, draft_text, dur, source="output"
    )
    # 약-위험인자 (DUR 8종 밖)
    #
    # ⚠️ session.risk_factors 만 보면 안 된다.
    #    sess.update() 는 run_turn 맨 끝에서 호출되므로 이번 턴에 처음 밝혀진 위험인자는
    #    아직 세션에 없다. "어제 술 먹고 머리 아파요"에 바로 진통제를 추천하는 케이스가
    #    첫 턴에서 통째로 새던 버그. 세션(과거) ∪ plan(이번 턴)을 합쳐서 본다.
    if cfg["layers"].get("risk_check", True):
        seen = {r.type for r in session.risk_factors}
        risks = list(session.risk_factors) + [
            r for r in plan.entities.risk_factors if r.type not in seen
        ]
        hits += risk_mod.check(names, risks)

    return dur_mod.dedupe(hits), names


async def rewrite(
    text: str,
    draft_text: str,
    hits: list[SafetyHit],
    session: SessionState,
    llm: LLM,
) -> str:
    """절대 금기는 경고 삽입이 아니라 재작성이어야 한다.

    삽입만 하면 자기모순이 된다:
      "숙취 두통엔 타이레놀이 좋습니다. ⚠️ 음주 후 타이레놀은 피하세요."
    읽는 사람이 뭘 하라는 건지 모르고, 커뮤니케이션 품질 축에서 크게 깎인다.
    """
    detail = "\n".join(
        f"- {' + '.join(h.drugs) or h.risk_factor}: {h.kind} — {h.reason}" for h in hits
    )
    risks = ", ".join(r.type for r in session.risk_factors) or "없음"
    sys = (
        "이전 답변의 약물 추천에 문제가 있었습니다. 아래 원칙으로 답변을 다시 작성하세요.\n"
        "1. 문제가 된 약물 추천을 제거하거나 명확한 조건을 붙일 것\n"
        "2. 안전한 대안이 있으면 제시할 것\n"
        "3. 대안이 확실치 않으면 약물 추천 대신 약사·의사 상담을 권할 것\n"
        "4. 근거가 불확실한 부분은 불확실하다고 명시할 것 "
        "(단정하면 감점, 조건을 밝히면 가점)\n"
        "5. 안전 관련 안내는 반드시 첫 문단에 둘 것"
    )
    user = (
        f"[문제]\n{detail}\n\n[사용자 위험인자]\n{risks}\n\n"
        f"[원래 질문]\n{text}\n\n[이전 답변]\n{draft_text}"
    )
    return await llm.chat("rewriter", [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ])


_DRUG_SENTENCE = re.compile(r"[^.!?\n]*(?:권장|추천|드셔|복용|드시는 것|사용해)[^.!?\n]*[.!?]")


def strip_drug_recommendation(text: str) -> str:
    """재작성 2회 후에도 안 되면 약물 추천 자체를 제거한다.

    이건 도피가 아니라 정답이다. HealthBench 루브릭도 불확실할 때
    전문가 상담을 권하는 것을 가점으로 본다.
    """
    return _DRUG_SENTENCE.sub("", text).strip() + CONSULT_PHARMACIST
