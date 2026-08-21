"""generation 단계 — 최종 답변을 만든다.

L2 에게는 retrieve_relevant_content 하나만 준다. 모델이 스스로 판단해서
memory 로 답하거나, 그 도구를 불러 근거를 받아 답한다.

프롬프트 설계 근거는 09 문서(HealthBench Consensus 축 분포)다.
  정확성 43.1% · 맥락인지 24.7% · 의사소통 21.2% · 지시순응 7.1% · 완전성 4.0%
완전성이 4% 라 "빠짐없이 길게"는 손해다. max_tokens 2048 상한도 같은 방향을 가리킨다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from retrieval import RETRIEVE_TOOL, RetrievalResult, run_retrieval

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

    # 검색할 도구가 없는 도메인(generic)이면 도구를 아예 주지 않는다.
    #
    # 도구를 주기만 하면 L2 는 그냥 자기 지식으로 답해 버린다 — 그것도 [1] 같은 인용을
    # 붙여서. 실측으로 확인했다. 근거 없는 인용은 정확성 축에 그대로 손해라서,
    # 라우터가 "검색이 필요한 도메인"이라고 판정했으면 호출을 강제한다.
    extra: dict[str, Any] | None = None
    if route.tools:
        extra = {
            "tools": [RETRIEVE_TOOL],
            "tool_choice": {
                "type": "function",
                "function": {"name": RETRIEVE_TOOL["function"]["name"]},
            },
        }

    data = await call_fm(convo, max_tokens, extra)
    choice = data["choices"][0]
    msg = choice["message"]
    calls = msg.get("tool_calls") or []

    if not calls:
        return (msg.get("content") or "").strip(), None

    # 모델이 근거가 필요하다고 판단했다. retrieval 단계를 돌려 돌려준다.
    convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
    result: RetrievalResult | None = None

    for tc in calls:
        fn = (tc.get("function") or {}).get("name") or ""
        raw = (tc.get("function") or {}).get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {}
        query = str(args.get("query") or "").strip()

        if fn != RETRIEVE_TOOL["function"]["name"] or not query:
            convo.append(
                {"role": "tool", "tool_call_id": tc.get("id") or "", "content": "{}"}
            )
            continue

        result = await run_retrieval(
            query, route.tools, call_fm, mcp, budget=budget,
            deadline=deadline, reserve=reserve,
        )
        log.info(
            "retrieval: status=%s items=%d calls=%d q=%r",
            result.status, len(result.items), result.tool_calls_used, query[:80],
        )
        convo.append(
            {"role": "tool", "tool_call_id": tc.get("id") or "", "content": result.as_prompt()}
        )

    # 근거를 받은 상태로 다시 답을 만든다. 이번엔 도구를 주지 않는다 — 답을 쓸 차례다.
    data2 = await call_fm(convo, max_tokens)
    content = (data2["choices"][0]["message"].get("content") or "").strip()
    return content, result
