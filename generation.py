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

import json
import os

from budget import answer_timeout, call_cap
from digest import DIGEST_MODE, digest
from retrieval import (
    EVIDENCE_BUDGET_CHARS,
    RETRIEVE_TOOL,
    RetrievalResult,
    render_evidence,
    run_retrieval,
)

# ── 근거를 어느 채널로 넣을 것인가 ───────────────────────────
#
#   system : system 메시지로 끼워 넣는다 (기존)
#   tool   : assistant tool_call + tool 결과 쌍을 우리가 만들어 넣는다
#
# 왜 이 스위치가 생겼나. 근거를 제대로 받은 문항에서도 답변에 [n] 인용이
# 하나도 안 붙었다 — 6문항 0/6. 프롬프트 문구를 강화해도 0/6 이었다.
# 이 모델은 chain-of-evidence 체크포인트이고, 대시보드가 문서화한 흐름은
# retrieve_relevant_content 라는 **도구 결과**로 근거를 받는 것이다.
# 인용 행동이 그 채널에 묶여 있다면 system 으로 아무리 잘 써 줘도 안 붙는다.
#
# tool 채널은 FM 호출을 늘리지 않는다. 모델이 도구를 부르기를 기다리는 대신
# 우리가 그 왕복을 미리 적어 넣는다 — 라우터가 이미 질의를 만들어 뒀으므로
# 모델에게 물어볼 것이 없다.
# 실측 (PIPELINE=harness, 5문항):
#   system 채널  인용 0/5 · 총 0개
#   tool   채널  인용 5/5 · 총 13개
# 프롬프트 문구를 강화해도 system 채널은 0/5 였다. 채널의 문제였다.
EVIDENCE_CHANNEL = os.environ.get("EVIDENCE_CHANNEL", "tool").strip().lower()

# 요약본을 담을 예산. 추출(EVIDENCE_BUDGET_CHARS=4000)보다 크게 잡는다 —
# 추출은 버려서 줄이고 요약은 줄여서 담는 것이라, 같은 예산이면 요약할 이유가 없다.
DIGEST_BUDGET_CHARS = int(os.environ.get("DIGEST_BUDGET_CHARS", "12000"))

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

# ── 근거 블록 머리말 ─────────────────────────────────────────
#
# 이전 문구는 모델에게 빠져나갈 구멍을 줬다 — "관련 없다고 판단되면 무시하고
# 아무것도 인용하지 말라". 실측에서 근거를 제대로 받은 문항까지 인용이 0/6 이었다.
#
# 그리고 압축 단계가 남기는 생략 표시("... more paragraphs omitted ...",
# "(N more retrieved items were not included ...)")를 어떻게 다뤄야 하는지
# 아무도 알려주지 않았다. 표시만 남기고 지시가 없으면 없는 것과 같다.
EVIDENCE_HEADER = """The numbered items below were retrieved for this question. They are the
only sources you may cite.

How to use them:
- When a sentence rests on one of the items, put its number at the end of that sentence:
  [1] or [2]. Cite the specific item, never a range, and never a number that is not listed.
- Copy numbers, doses, prices, dates, codes and article numbers EXACTLY as the evidence
  writes them. Do not round, convert units, or silently correct what looks wrong.
- Carry over the conditions attached to a fact. "Covered" and "covered only for patients
  over 65" are different answers.
- An item may be partial: text marked "... more paragraphs omitted ...", or a line saying
  some retrieved items were not included. Use what is present and do NOT assume the missing
  part agrees with you. If the omitted part is what the question hinges on, say which piece
  you could not verify.
- If the evidence contradicts what you were about to say, the evidence wins.
- If the evidence covers only part of the question, answer the rest from general medical
  knowledge and cite nothing for that part. Never describe your own search, never say the
  evidence was unhelpful, and never apologise for it — the reader wants the answer.
"""


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
    answer_cap: float = 45.0,
    answer_floor: float = 25.0,
) -> tuple[str, RetrievalResult | None]:
    """generation 단계를 돌린다. 모델이 도구를 부르면 그 안에서 retrieval 을 실행한다.

    `reserve` 는 검색을 언제 끊을지의 기준(답변+검증 몫)이고, `answer_cap` 은
    **최종 답변 호출 하나**에 걸 상한이다. 답변 호출에는 reserve 를 걸지 않는다 —
    이 호출이 곧 답이라, 여기서 아껴 봐야 아낄 대상이 없다. 검증은 자기 몫이
    남지 않으면 규칙 검사만 돌리도록 스스로 물러난다.
    """
    convo: list[dict] = [{"role": "system", "content": generation_system(route)}]
    convo.extend(messages)

    # 검색할 게 없는 도메인(generic)이면 그대로 답한다.
    if not route.tools:
        data = await call_fm(
            convo, max_tokens, timeout=answer_timeout(deadline, answer_cap, answer_floor)
        )
        return (data["choices"][0]["message"].get("content") or "").strip(), None

    # 원래는 여기서 모델에게 retrieve_relevant_content 를 주고, 모델이 질의를 만들어
    # 호출해 오기를 기다렸다. 그 왕복이 FM 호출을 한 번 더 썼고, 요청당 호출 수가
    # 곧 지연이라 통째로 걷어냈다. 라우터가 이미 self-contained 질의를 만들어 둔다.
    #
    # (도구를 주기만 하면 L2 가 검색을 건너뛰고 [1] 인용을 지어내던 문제도 같이 사라진다.
    #  이제 검색 여부는 모델이 아니라 라우터가 정한다.)
    query = (route.search_query or "").strip() or _last_user(messages)
    _ctx = route.search_ctx() if hasattr(route, "search_ctx") else {}
    # 라우터가 두 언어를 실제로 만들어 냈는지 보이게 한다. 비어 있으면 언어 계약이
    # 통째로 무력해지는데, 로그가 없으면 그 사실을 알 수 없다.
    log.info("route ctx: %s", {k: (v[:40] if isinstance(v, str) else v) for k, v in _ctx.items() if v})
    result = await run_retrieval(
        query, route.tools, call_fm, mcp, budget=budget,
        deadline=deadline, reserve=reserve,
        # 도구마다 필요한 언어가 달라서, 라우터가 만들어 둔 한/영 변형을 같이 넘긴다.
        ctx=route.search_ctx() if hasattr(route, "search_ctx") else None,
    )
    log.info(
        "retrieval: status=%s items=%d calls=%d q=%r",
        result.status, len(result.items), result.tool_calls_used, query[:80],
    )
    # 추출로 안 줄어드는 크기가 왔고 시간이 있으면, 청크를 **병렬로** 한 라운드
    # 요약한다. 순차 refine 이 아니라 map 인 이유는 벽시계 때문이다 — 순차면
    # 호출 수만큼 지연이 쌓이고, 이 저장소에서 지연은 곧 점수다.
    if DIGEST_MODE != "off" and result.items:
        try:
            head = len(result.status) + len(result.note) + 16
            # 요약본은 추출본보다 넉넉한 예산을 쓴다. 추출은 버려서 줄이지만
            # 요약은 줄여서 담는 것이라, 같은 예산이면 요약의 이점이 사라진다.
            # 게이트웨이 입력 상한은 ~400KB 라 자리는 남는다. 대가는 지연과
            # thinking 몫이고, 그건 DIGEST_BUDGET_CHARS 로 조절한다.
            packed, n_calls = await digest(
                result.items,
                query,
                budget=max(0, DIGEST_BUDGET_CHARS - head),
                call_fm=call_fm,
                deadline=deadline,
            )
            if n_calls:
                result.rendered = render_evidence(
                    result.status, result.note, packed, len(result.items)
                )
        except Exception as e:  # noqa: BLE001 — 요약이 죽어도 근거는 있어야 한다
            log.warning("다이제스트 실패: %s: %s", type(e).__name__, str(e)[:200])

    _ev = result.as_prompt()
    log.info(
        "retrieval trace: %s",
        " | ".join(result.trace)[:400],
    )
    log.info(
        "evidence: 원본 %d자 -> 프롬프트 %d자 (항목 %d)",
        sum(len(e.text) for e in result.items), len(_ev), len(result.items),
    )

    evidence_text = result.as_prompt()
    if EVIDENCE_CHANNEL == "tool":
        # 모델이 retrieve_relevant_content 를 부르고 결과를 받은 것처럼 대화를
        # 적어 넣는다. 실제 왕복은 없다 — 질의는 라우터가 이미 만들어 뒀다.
        call_id = "call_evidence_0"
        convo.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": RETRIEVE_TOOL["function"]["name"],
                            "arguments": json.dumps({"query": query}, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        convo.append(
            {"role": "tool", "tool_call_id": call_id, "content": evidence_text}
        )
        convo.insert(1, {"role": "system", "content": EVIDENCE_HEADER.strip()})
    else:
        # 근거를 마지막 사용자 발화 바로 앞에 끼워 넣는다.
        convo.insert(
            max(1, len(convo) - 1),
            {"role": "system", "content": EVIDENCE_HEADER + evidence_text},
        )
    extra = {"tools": [RETRIEVE_TOOL]} if EVIDENCE_CHANNEL == "tool" else None
    data = await call_fm(
        convo,
        max_tokens,
        extra,
        timeout=answer_timeout(deadline, answer_cap, answer_floor),
    )
    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()

    if not content and choice["message"].get("tool_calls"):
        # 도구를 쥐어 주면 답 대신 도구를 또 부를 수 있다. 그러면 content 가 비고
        # 그 문항은 0점이다. 도구를 뺀 채 근거를 system 으로 옮겨 한 번만 되살린다.
        log.info("답변 대신 도구를 불렀다 — 도구 없이 다시 받는다")
        retry = [m for m in convo if m.get("role") != "tool" and not m.get("tool_calls")]
        retry.insert(
            max(1, len(retry) - 1),
            {"role": "system", "content": EVIDENCE_HEADER + evidence_text},
        )
        data = await call_fm(
            retry,
            max_tokens,
            timeout=answer_timeout(deadline, answer_cap, answer_floor),
        )
        content = (data["choices"][0]["message"].get("content") or "").strip()

    return content, result


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""
