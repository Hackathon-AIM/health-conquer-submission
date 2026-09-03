"""세션 상태 — 턴과 턴을 잇는 되먹임.

3턴 대화에서 1턴에 얻은 정보를 3턴에서 못 쓰면 개인화가 안 된다.

읽는 곳이 셋
  · L2 분류기  — 이미 아는 건 다시 안 묻게 (불필요한 되묻기 = 감점)
  · L1b DUR    — 기존 복용약과의 상호작용
  · L4b risk   — 위험인자와의 충돌 (v2에서 추가된 경로)

⭐ 여기가 프론티어를 이기는 지점
  GPT/Claude도 음주+아세트아미노펜 상호작용을 안다. 문제는 사용자가 1턴에 흘린
  "어제 술 마셨다"를 3턴 뒤 약 추천에 반영하느냐인데, 그건 확률적이다.
  우리는 슬롯에 박아두고 규칙으로 강제하므로 100% 걸린다. 구조가 기억력을 이긴다.
"""

from __future__ import annotations

import re

from . import entities as ent
from .contracts import QueryPlan, RiskFactor, SessionState

# "복용 중"을 나타내는 표현. 이게 있어야 medication_names 로 승격된다.
_TAKING = re.compile(
    r"(먹고\s*있|복용\s*(중|하고)|드시고\s*있|처방\s*(받|중)|"
    r"매일\s*(먹|복용)|계속\s*(먹|복용)|장기\s*복용)"
)


def update(session: SessionState, user_text: str, plan: QueryPlan, answer: str) -> SessionState:
    """응답 후 대화에서 슬롯을 추출해 누적한다."""
    e = plan.entities

    # 나이 (규칙)
    if session.age is None:
        a = ent.extract_age(user_text)
        if a is not None:
            session.age = a

    # 위험인자 — 타입 단위로 중복 제거하며 누적
    have = {r.type for r in session.risk_factors}
    for r in e.risk_factors:
        if r.type not in have:
            session.risk_factors.append(r)
            have.add(r.type)
        if r.type == "pregnancy":
            session.pregnant = True

    # 복용약 — '복용 중'과 '문의 대상'을 구분한다.
    #
    # ⚠️ "타이레놀 먹어도 되나요?" 는 문의지 복용이 아니다.
    #    구분 없이 medication_names 에 넣으면 다음 턴 DUR 이
    #    "이 사람은 타이레놀 복용 중"으로 오판해 없는 금기를 만들어낸다.
    #    복용 표현이 있거나 사용자가 복용 사실을 밝힌 경우에만 복용약으로 승격한다.
    taking = bool(_TAKING.search(user_text))
    for d in e.drugs:
        if taking:
            if d.name not in session.medication_names:
                session.medication_names.append(d.name)
            for c in d.ingredient_codes:
                if c not in session.medications:
                    session.medications.append(c)
        else:
            if d.name not in session.asked_about:
                session.asked_about.append(d.name)

    # 증상 지속기간
    if e.temporal and not session.symptom_duration:
        session.symptom_duration = e.temporal

    # 대화 이력 (컨텍스트 예산 때문에 길이를 제한한다)
    session.history.append({"role": "user", "content": user_text[:800]})
    session.history.append({"role": "assistant", "content": answer[:1200]})
    session.history = session.history[-8:]

    return session


def new(**kwargs) -> SessionState:
    return SessionState(**kwargs)
