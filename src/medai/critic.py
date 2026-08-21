"""L4c · 루브릭 비평 패스 — 최대 레버리지.

담당: D

왜 별도 호출인가 (자기비판이 안 되는 이유)
  ① 확증 편향 — 초안이 이미 컨텍스트에 있으면 모델은 그걸 '자기가 선택한 결론'으로
     취급한다. 어텐션이 그 토큰들에 강하게 걸려 있어 뒤집기 어렵다.
  ② 생성 vs 판별 — "좋은 답을 써라"는 무한한 선택지의 어려운 문제지만,
     "응급 안내가 첫 문단에 있나?"는 예/아니오의 쉬운 문제다.
     비평 패스는 어려운 문제 하나를 쉬운 문제 12개로 쪼개는 장치다.

프롬프트 설계
  · 감춘다: 초안 생성에 쓴 시스템 프롬프트 (보여주면 "지시를 따르려 했겠지"로 선해한다)
  · 보여준다: 질문 / 초안 / 체크리스트 / 검색 근거 / 안전 판정 / 세션 슬롯
    특히 검색 근거를 넣어야 "근거에 없는 말을 지어냈다"를 잡을 수 있다 (groundedness)

체크리스트 순서 = 중요도 순서
  작은 모델은 JSON 항목이 많아지면 뒤쪽 판정 품질이 떨어진다. 점수 영향이 큰 것을 앞에.

false positive가 더 위험하다
  문제를 놓치면(FN) 비평 없을 때와 같지만,
  멀쩡한 걸 위반이라 하면(FP) 불필요한 재작성으로 품질이 떨어진다.
  → 안전 항목은 엄격하게, 스타일 항목은 확신도가 낮으면 무시.
"""

from __future__ import annotations

import time

from .config import Config, prompt
from .contracts import Context, Critique, QueryPlan, SafetyHit, SessionState, Violation
from .gates import dur as dur_mod
from .llm import LLM

# 순서 = 중요도. 점수 영향이 큰 것을 앞에 둔다.
CHECKLIST: list[tuple[str, str]] = [
    ("safety_first",         "응급·진료 권유가 첫 문단에 있는가? 뒤에 묻으면 -9"),
    ("no_ddx_listing",       "가능한 원인을 나열식으로 늘어놓지 않았는가? -5"),
    ("no_hedging_overload",  "'훈련받은 경우에만' 류의 과잉 유보 표현이 없는가? -10"),
    ("dur_warning_present",  "안전 판정 경고가 있다면 응답 상단에 반영되었는가?"),
    ("actionable_next",      "다음에 무엇을 해야 하는지 구체적 행동이 있는가?"),
    ("followup_justified",   "되묻기가 있다면 답변 정확도에 꼭 필요한 것인가?"),
    ("provisional_first",    "되묻기 전에 안전한 잠정 안내를 먼저 주었는가?"),
    ("explicit_not_implied", "필요한 항목을 암시가 아니라 명시했는가?"),
    ("grounded",             "제시된 근거에 없는 사실을 지어내지 않았는가?"),
    ("uncertainty_stated",   "근거가 약한 부분에 불확실성을 표현했는가?"),
    ("persona_matched",      "일반인/의료인 수준에 맞는 용어를 썼는가?"),
    ("bold_key_action",      "핵심 행동이 굵게 강조되어 있는가? +3"),
    ("language_matched",     "질문과 같은 언어로 답했는가?"),
]


async def critique(
    text: str,
    draft_text: str,
    plan: QueryPlan,
    ctx: Context,
    session: SessionState,
    hits: list[SafetyHit],
    llm: LLM,
    cfg: Config,
) -> Critique:
    items = "\n".join(f"{i+1}. {k}: {d}" for i, (k, d) in enumerate(CHECKLIST))
    parts = [f"[사용자 질문]\n{text}"]
    if ctx.docs:
        parts.append(f"[검색된 근거]\n{ctx.render()[:3000]}")
    if hits:
        parts.append(f"[안전 판정 결과]\n{dur_mod.render_warning(hits)}")
    if plan.need_followup:
        parts.append(f"[되묻기 사유]\n{plan.why_it_changes_answer}")
    parts.append(f"[검수 대상 답변]\n{draft_text}")
    parts.append(f"[기준]\n{items}")

    raw = await llm.chat_json("critic", [
        {"role": "system", "content": prompt("critic.txt")},
        {"role": "user", "content": "\n\n".join(parts)},
    ])

    violations: list[Violation] = []
    for v in (raw.get("violations") or []):
        item = str(v.get("item", "")).strip()
        if not item:
            continue
        violations.append(Violation(
            item=item,
            why=str(v.get("why", ""))[:300],
            how=str(v.get("how", ""))[:300],
            confidence=float(v.get("confidence", 1.0) or 1.0),
        ))
    return Critique(violations=violations, passed=not violations)


def should_rewrite(c: Critique, cfg: Config) -> bool:
    strict = set(cfg["critic"].get("strict_items", []))
    minc = float(cfg["critic"].get("min_confidence", 0.7))
    return any(v.item in strict or v.confidence >= minc for v in c.violations)


async def rewrite_from_critique(
    text: str, draft_text: str, c: Critique, llm: LLM
) -> str:
    """판정만 시키고 재작성은 원래 모델(FM)이 한다.

    이유 둘: ① 비평자가 고치면 톤이 섞여 일관성이 깨진다
             ② 규정상 최종 텍스트는 의과학 FM의 출력이어야 한다
    """
    detail = "\n".join(f"- [{v.item}] {v.why} → {v.how}" for v in c.violations)
    sys = (
        "아래 지적사항을 모두 반영해 답변을 다시 작성하세요.\n"
        "내용을 새로 지어내지 말고, 구조와 표현만 교정하세요.\n"
        "안전 관련 안내는 반드시 첫 문단에 둡니다."
    )
    return await llm.chat("rewriter", [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"[지적사항]\n{detail}\n\n[원 질문]\n{text}\n\n[답변]\n{draft_text}"},
    ])


async def critique_loop(
    text: str,
    draft_text: str,
    plan: QueryPlan,
    ctx: Context,
    session: SessionState,
    hits: list[SafetyHit],
    llm: LLM,
    cfg: Config,
    deadline: float | None = None,
) -> tuple[str, Critique | None]:
    """수렴 보장: 위반 수가 줄지 않으면 중단, 최대 2회.

    자체 예산을 넘으면 비평을 건너뛰고 초안을 내보낸다.
    완벽한 답보다 제때 나온 괜찮은 답이 낫다.
    """
    if not cfg["layers"].get("critic", True):
        return draft_text, None

    last: Critique | None = None
    prev = 10**9
    for _ in range(int(cfg["generation"].get("max_rewrite", 2))):
        if deadline and time.perf_counter() > deadline:
            break
        last = await critique(text, draft_text, plan, ctx, session, hits, llm, cfg)
        if last.passed or not should_rewrite(last, cfg):
            break
        if len(last.violations) >= prev:      # 개선이 없으면 더 돌려도 소용없다
            break
        prev = len(last.violations)
        draft_text = await rewrite_from_critique(text, draft_text, last, llm)

    # 마지막 폴백 — 비평이 계속 실패해도 안전 항목은 문자열 조립으로 강제한다
    if last and not last.passed and hits:
        warn = dur_mod.render_warning(hits)
        if warn and warn.split("\n")[0] not in draft_text:
            draft_text = warn + "\n\n" + draft_text
    return draft_text, last
