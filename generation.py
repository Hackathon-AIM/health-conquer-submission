"""generation 단계 — 최종 답변을 만든다.

L2 에게는 retrieve_relevant_content 하나만 준다. 모델이 스스로 판단해서
memory 로 답하거나, 그 도구를 불러 근거를 받아 답한다.

프롬프트 설계 근거는 09 문서(HealthBench Consensus 축 분포)다.
  정확성 43.1% · 맥락인지 24.7% · 의사소통 21.2% · 지시순응 7.1% · 완전성 4.0%
완전성이 4% 라 "빠짐없이 길게"는 손해다. max_tokens 2048 상한도 같은 방향을 가리킨다.
"""

from __future__ import annotations

import logging
from typing import Any

from retrieval import RetrievalResult, run_retrieval

log = logging.getLogger("generation")

_PERSONA = {
    "layperson": (
        "The reader is a member of the public. Use plain Korean, expand any term you must use, "
        "and lead with what they should actually do."
    ),
    "practitioner": (
        "The reader handles claims/pharmacy/administrative work. Be precise about codes, "
        "criteria and effective dates, and say plainly whether it is billable or not."
    ),
    "clinician": (
        "The reader is a clinician managing a patient. Use clinical terminology directly and "
        "give recommendation strength / evidence level when the source states it."
    ),
}

_URGENCY = {
    "emergency": (
        "RED FLAGS ARE PRESENT. Open by telling them to seek emergency care now (119 / 응급실), "
        "state the specific signs that make this urgent, and what to do meanwhile. "
        "Do NOT ask follow-up questions. Keep it short and unambiguous."
    ),
    "conditional": (
        "This is urgent ONLY under specific conditions. Name those red flags concretely and say "
        "what to do if they appear; otherwise give the ordinary guidance."
    ),
    "routine": (
        "This is not urgent. Do NOT tack on a generic 'see a doctor' line — say it only if there "
        "is a specific reason to, and name that reason."
    ),
}

BASE_SYSTEM = """You are a Korean medical assistant answering a real person's question.

{persona}

{urgency}

How to answer:
- Accuracy first. Cite as [1], [2] ONLY items that were actually handed to you as numbered
  evidence. Never write a bracketed citation from memory, and never claim you "searched" or
  "checked the guideline" when you did not — an unsupported citation is worse than none.
- If the evidence is thin or absent, say plainly what is known, what the evidence did not settle,
  and answer from general knowledge clearly marked as such. Never invent a guideline, article
  number, price, or code.
- Be complete enough to be useful, then stop. Length is not a virtue here; a tight answer beats a
  long one. Do not restate the question or add filler openers.
- Answer in {lang_name}.
{extra}"""

DATE_NOTE = """- The question is pinned to a specific date. The sources you can read are CURRENT text
  only. If a rule took effect after the date in question, say explicitly that it is not the rule
  that applied then, rather than answering as if it were."""


def generation_system(route: Any) -> str:
    return BASE_SYSTEM.format(
        persona=_PERSONA.get(route.persona, _PERSONA["layperson"]),
        urgency=_URGENCY.get(route.urgency, _URGENCY["routine"]),
        lang_name="Korean" if route.lang == "ko" else "English",
        extra=DATE_NOTE if route.date_sensitive else "",
    )


async def generate(
    messages: list[dict],
    route: Any,
    call_fm,
    mcp,
    max_tokens: int,
    budget: int = 6,
    deadline=None,
    reserve: float = 25.0,
) -> tuple[str, RetrievalResult | None]:
    """generation 단계를 돌린다. 모델이 도구를 부르면 그 안에서 retrieval 을 실행한다."""
    convo: list[dict] = [{"role": "system", "content": generation_system(route)}]
    convo.extend(messages)

    # 검색할 게 없는 도메인(generic)이면 그대로 답한다.
    if not route.tools:
        data = await call_fm(convo, max_tokens)
        return (data["choices"][0]["message"].get("content") or "").strip(), None

    # 원래는 여기서 모델에게 retrieve_relevant_content 를 주고, 모델이 질의를 만들어
    # 호출해 오기를 기다렸다. 그 왕복이 FM 호출을 한 번 더 썼고, 요청당 호출 수가
    # 곧 지연이라 통째로 걷어냈다. 라우터가 이미 self-contained 질의를 만들어 둔다.
    #
    # (도구를 주기만 하면 L2 가 검색을 건너뛰고 [1] 인용을 지어내던 문제도 같이 사라진다.
    #  이제 검색 여부는 모델이 아니라 라우터가 정한다.)
    query = (route.search_query or "").strip() or _last_user(messages)
    result = await run_retrieval(
        query, route.tools, call_fm, mcp, budget=budget,
        deadline=deadline, reserve=reserve,
    )
    log.info(
        "retrieval: status=%s items=%d calls=%d q=%r",
        result.status, len(result.items), result.tool_calls_used, query[:80],
    )

    # 근거는 마지막 사용자 발화 바로 앞에 끼워 넣는다. tool 메시지로 주려면
    # tool_call_id 짝을 맞춰야 하는데, 평범한 컨텍스트로 줘도 모델은 똑같이 읽는다.
    convo.insert(
        max(1, len(convo) - 1),
        {
            "role": "system",
            "content": (
                "Retrieved evidence for this question. Cite these as [1], [2] and cite "
                "nothing else. If they do not answer it, say so.\n\n" + result.as_prompt()
            ),
        },
    )
    data = await call_fm(convo, max_tokens)
    content = (data["choices"][0]["message"].get("content") or "").strip()
    return content, result


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""
