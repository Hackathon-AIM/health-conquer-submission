"""L2 의 2단계 구조 — retrieval 단계와 generation 단계.

대시보드 가이드가 못박은 구조다. L2 는 한 번에 생각·검색·답변을 하지 않고
두 단계를 나누며, 단계마다 모델을 따로 호출한다.

  retrieval  — MCP 도구 + finalize_retrieval 만 준다. 최종 답변을 쓰지 않고,
               관련 있다고 판단한 항목의 cite_uid 만 보고한다.
  generation — retrieve_relevant_content 하나만 준다. 모델이 필요하다고 판단하면
               그 도구를 부르고, 그 안에서 retrieval 단계를 돌려 근거를 돌려준다.

실측 메모:
  - cite_uid 는 index_get_page_content 결과에만 실려 온다. relevant_nodes 만 보고
    finalize_retrieval 을 부르면 인용할 게 없다
  - L2 는 한국어 질문을 영어 검색 쿼리로 알아서 바꿔 던진다
  - max_tokens 상한이 2048 이라 단계마다 예산을 아껴 써야 한다
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from budget import call_cap
from toolspec import call_key, normalize_args

# 도구 언어 계약을 적용할 것인가. 0 이면 모델이 낸 인자를 그대로 쓴다(A/B 용).
TOOLSPEC_NORMALIZE = os.environ.get("TOOLSPEC_NORMALIZE", "1") == "1"

log = logging.getLogger("retrieval")

# ── 도구별 시간 상한 ──────────────────────────────────────────
# 21종을 같은 상한으로 다루면 안 된다. 코드 조회는 1초 안에 오고, 페이지 원문·SQL·
# 벡터 검색은 십수 초가 걸린다. 느린 쪽에 맞춰 상한을 잡으면 빠른 도구가 죽을 때
# 그 대기가 예산을 다 먹고, 빠른 쪽에 맞추면 느린 도구는 항상 실패한다.
FAST_TOOLS = {
    "kcd_get_name",
    "kcd_search_codes",
    "openapi_hira_disease_check_code",
    "openapi_hira_get_drug_price",
    "openapi_mfds_check_drug_permission",
    "openapi_mfds_find_drugs_by_ingredient",
    "openapi_law_search",
    "openapi_law_list_articles",
    "openapi_law_get_article",
    "rag_get_all_data_sources",
    "rag_get_data_source_detail",
    "index_list_documents",
}
MCP_FAST_CAP_S = float(os.environ.get("MCP_FAST_CAP_S", "8"))
MCP_SLOW_CAP_S = float(os.environ.get("MCP_SLOW_CAP_S", "18"))

# retrieval 단계의 FM 왕복 하나에 걸 상한.
RETRIEVAL_STEP_CAP_S = float(os.environ.get("RETRIEVAL_STEP_CAP_S", "20"))

# 도구 결과를 대화에 얹을 때의 길이. 이게 크면 다음 스텝의 프롬프트가 그만큼
# 부풀고, 부푼 프롬프트는 그대로 지연이 된다 — 3스텝이면 앞 두 스텝의 결과를
# 통째로 다시 보내는 셈이다.
#
# 입력 컨텍스트가 모자라서가 아니다(창은 131k 다). 근거가 길수록 모델이
# 사고에 쓰는 몫이 커지고 — CoEval 문서도 RAG 컨텍스트가 있으면 thinking
# 비중이 올라간다고 적는다 — 그 몫은 답변 몫에서 나온다.
TOOL_RESULT_CHARS = int(os.environ.get("TOOL_RESULT_CHARS", "6000"))

# generation 단계에 넘길 근거 블록의 총량. 항목 수가 아니라 글자 수로 막는다 —
# 항목 하나가 20페이지 원문일 수도 있기 때문이다.
EVIDENCE_BUDGET_CHARS = int(os.environ.get("EVIDENCE_BUDGET_CHARS", "4000"))

# FM 왕복 상한. 도구 호출 예산과 별개로, 모델이 도구를 안 부르고 맴돌 때를 끊는다.
RETRIEVAL_MAX_STEPS = int(os.environ.get("RETRIEVAL_MAX_STEPS", "4"))


def tool_cap(name: str) -> float:
    return MCP_FAST_CAP_S if name in FAST_TOOLS else MCP_SLOW_CAP_S

# 도구 설명 전문을 다 넣으면 프롬프트가 부푼다. 앞부분에 사용 조건이 다 적혀 있다.
DESC_LIMIT = 900

FINALIZE = "finalize_retrieval"
FINALIZE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FINALIZE,
        "description": (
            "Submit your final citation selection and end the retrieval phase. "
            "Call this only: once you have gathered enough evidence to answer the query; "
            "the query does not need any retrieval; "
            "or you exhausted the tool call budget and must end the retrieval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["sufficient", "partial", "no_evidence"]},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cite_uid": {"type": "string"},
                            "relevance_score": {"type": "number"},
                        },
                        "required": ["cite_uid", "relevance_score"],
                        "additionalProperties": False,
                    },
                },
                "note": {"type": "string"},
            },
            "required": ["status", "items", "note"],
            "additionalProperties": False,
        },
    },
}

RETRIEVE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "retrieve_relevant_content",
        "description": (
            "Retrieve relevant content to ground your answer. "
            "Pass a single, self-contained query."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

RETRIEVAL_SYSTEM = """You are in the RETRIEVAL phase. You do NOT write an answer here.

Gather the evidence needed to answer the user's query using the tools, then report which items
are relevant by calling {finalize}.

Rules:
- Search first to locate material, then OPEN the pages/articles to read the actual text.
  Only opened content carries a cite_uid, and only a cite_uid can be cited.
- Prefer specific, descriptive search queries over short keywords. English queries work well
  against the guideline corpus even when the user wrote Korean.
- You have at most {budget} tool calls. When they run out, call {finalize} with what you have.
- Call {finalize} with status "no_evidence" if the query needs no lookup, and with "partial"
  if you found something but it does not fully settle the question.
- In "note", state briefly what is still missing or what caveat the answer must carry
  (for example: the retrieved rule took effect after the date the user asked about)."""


@dataclass
class Evidence:
    """열어본 항목 하나. cite_uid 가 있는 것만 인용할 수 있다."""

    cite_uid: str
    title: str = ""
    url: str = ""
    source_type: str = ""
    text: str = ""
    relevance: float = 0.0


@dataclass
class RetrievalResult:
    status: str = "no_evidence"  # sufficient | partial | no_evidence
    note: str = ""
    items: list[Evidence] = field(default_factory=list)
    tool_calls_used: int = 0
    trace: list[str] = field(default_factory=list)

    def as_prompt(self, limit: int = 1400, budget: int | None = None) -> str:
        """generation 단계에 넘길 형태. 대시보드 예시의 모양을 따른다.

        `budget` 은 근거 블록 **전체**의 글자 상한이다. 항목 하나가 20페이지
        원문일 수 있어서 항목 수로는 못 막는다. 남은 몫을 항목들이 나눠 갖고,
        더 담을 수 없으면 몇 개를 못 실었는지 밝힌다 — 조용히 자르면 모델이
        가진 근거가 전부라고 믿는다.
        """
        budget = EVIDENCE_BUDGET_CHARS if budget is None else budget
        lines = [f"status: {self.status}"]
        if self.note:
            lines.append(f"note: {self.note}")
        used = sum(len(x) for x in lines)
        shown = 0
        for i, ev in enumerate(self.items, 1):
            head = []
            head.append("")
            head.append(f"[{i}]")
            if ev.source_type:
                head.append(f"source_type: {ev.source_type}")
            if ev.url:
                head.append(f"url: {ev.url}")
            if ev.title:
                head.append(f"title: {ev.title}")
            head_len = sum(len(x) + 1 for x in head)
            room = budget - used - head_len
            if room <= 200:
                break
            text = ev.text[: min(limit, room)]
            lines.extend(head)
            lines.append(f"content: {text}")
            used += head_len + len(text) + 9
            shown += 1
        dropped = len(self.items) - shown
        if dropped > 0:
            lines.append(
                f"\n({dropped} more retrieved items were not included here "
                f"because of the evidence budget.)"
            )
        return "\n".join(lines)


def to_openai_tools(mcp_tools: list[dict], names: list[str]) -> list[dict]:
    """MCP inputSchema 를 OpenAI function 형식으로. 스키마가 이미 호환된다."""
    by_name = {t["name"]: t for t in mcp_tools}
    out = []
    for n in names:
        t = by_name.get(n)
        if not t:
            log.warning("알 수 없는 도구: %s", n)
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": (t.get("description") or "")[:DESC_LIMIT],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _harvest(payload: Any, out: dict[str, Evidence]) -> None:
    """도구 결과 어디에 있든 cite_uid 를 가진 것을 주워 담는다.

    소스마다 모양이 다르다 — index 계열은 최상위에 cite_uid 와 pages[] 를 두고,
    rag/openapi 계열은 items[] 안에 하나씩 담아 준다.
    """
    if isinstance(payload, list):
        for x in payload:
            _harvest(x, out)
        return
    if not isinstance(payload, dict):
        return

    uid = payload.get("cite_uid")
    if isinstance(uid, str) and uid:
        text = ""
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            parts = []
            for p in pages:
                if not isinstance(p, dict):
                    continue
                parts.append(str(p.get("text") or ""))
                # 플로우차트는 경로가 이미 텍스트로 펴져 있다. 임상 알고리즘 질문에 그대로 쓴다.
                for ch in p.get("charts") or []:
                    for path in (ch or {}).get("paths") or []:
                        parts.append(f"[flowchart] {path}")
            text = "\n".join(x for x in parts if x)
        if not text:
            for k in ("content", "text", "body", "row", "snippet"):
                v = payload.get(k)
                if v:
                    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    break
        prev = out.get(uid)
        if prev is None or (len(text) > len(prev.text)):
            out[uid] = Evidence(
                cite_uid=uid,
                title=str(payload.get("title") or payload.get("doc_title") or ""),
                url=str(payload.get("url") or payload.get("source_url") or ""),
                source_type=str(payload.get("source_type") or ""),
                text=text,
            )

    for v in payload.values():
        if isinstance(v, (dict, list)):
            _harvest(v, out)


async def run_retrieval(
    query: str,
    tool_names: list[str],
    call_fm,
    mcp,
    budget: int = 6,
    step_tokens: int = 900,
    deadline=None,
    reserve: float = 25.0,
    ctx: dict | None = None,
) -> RetrievalResult:
    """retrieval 단계 — 도구를 돌려 근거를 모으고 cite_uid 를 확정한다.

    `deadline` 을 주면 남은 시간을 보며 스스로 멈춘다. 답을 쓸 시간(`reserve`)은
    반드시 남긴다 — 근거를 더 모으다 답을 못 쓰면 그 문항은 0점이다.
    """
    res = RetrievalResult()
    if not tool_names:
        res.status = "no_evidence"
        res.note = "no retrieval tools for this domain"
        return res

    try:
        # 도구 목록도 시간에 묶는다. 여기서 막히면 검색을 시작도 못 하고 예산만 탄다.
        mcp_tools = await mcp.list_tools(
            timeout=call_cap(deadline, MCP_FAST_CAP_S, reserve=reserve)
        )
    except Exception as e:  # noqa: BLE001 — 검색이 죽어도 답변은 해야 한다
        log.warning("MCP 도구 목록 실패: %s", e)
        res.note = "retrieval unavailable"
        return res

    tools = to_openai_tools(mcp_tools, tool_names) + [FINALIZE_TOOL]
    messages: list[dict] = [
        {
            "role": "system",
            "content": RETRIEVAL_SYSTEM.format(finalize=FINALIZE, budget=budget),
        },
        {"role": "user", "content": query},
    ]
    harvested: dict[str, Evidence] = {}
    ctx = ctx or {}
    # 같은 요청 안에서 같은 호출을 두 번 하지 않는다. 모델이 같은 도구를 조금씩
    # 다른 표현으로 반복해 부르는 것을 실측에서 봤고, 그건 예산만 태운다.
    done: set[str] = set()

    # budget 은 **실제 MCP 도구 호출 수**다. 예전에는 이 값이 FM 왕복 수였고,
    # 모델이 한 스텝에 도구를 3개 부르면 예산 3에 9번이 나갔다. 이름과 주석은
    # "도구 호출 수" 라고 말하고 있었으므로 이쪽이 원래 의도다.
    steps_left = max(1, RETRIEVAL_MAX_STEPS)

    while res.tool_calls_used < budget and steps_left > 0:
        steps_left -= 1
        if deadline is not None and deadline.expired(reserve=reserve):
            log.info("시간 예산으로 retrieval 조기 종료 (남은 %.0fs)", deadline.remaining())
            res.note = (res.note + " " if res.note else "") + "retrieval cut short by time budget"
            break
        try:
            data = await call_fm(
                messages,
                step_tokens,
                {"tools": tools},
                timeout=call_cap(deadline, RETRIEVAL_STEP_CAP_S, reserve=reserve),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("retrieval 스텝 실패: %s", e)
            break

        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            break

        # assistant 턴을 히스토리에 그대로 되돌려 넣어야 tool 결과가 짝이 맞는다.
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )

        finalized = False
        for tc in calls:
            fn = (tc.get("function") or {}).get("name") or ""
            raw_args = (tc.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}

            if fn == FINALIZE:
                res.status = str(args.get("status") or "partial")
                res.note = str(args.get("note") or "")
                for it in args.get("items") or []:
                    uid = str((it or {}).get("cite_uid") or "")
                    ev = harvested.get(uid)
                    if ev:
                        ev.relevance = float((it or {}).get("relevance_score") or 0.0)
                        res.items.append(ev)
                res.trace.append(f"{FINALIZE}(status={res.status}, items={len(res.items)})")
                finalized = True
                break

            # 한 스텝에 도구를 여러 개 부를 수 있다. 예산이나 시간을 이미 썼으면
            # 실제로는 부르지 않는다 — 다만 tool 응답 자체는 반드시 채워 넣는다.
            # tool_call 하나에 짝이 되는 tool 메시지가 없으면 다음 스텝 요청이 400 이다.
            # ── 인자를 도구의 언어 계약에 맞춘다 ──────────────────
            # 21종은 코퍼스 언어가 제각각이고 한 도메인 서브셋 안에서도 갈린다.
            # 틀린 언어로 부르면 0건이 오는데 비용은 성공한 호출과 똑같다.
            # 맞출 수 없으면 부르지 않는 편이 예산에 이득이다.
            if TOOLSPEC_NORMALIZE:
                before = dict(args)
                args, skip = normalize_args(fn, args, ctx)
                changed = {k: v for k, v in args.items() if before.get(k) != v}
                if changed:
                    # 무엇을 왜 바꿨는지 보이지 않으면 이 계약이 맞는지 나중에 검증할 수 없다.
                    log.info(
                        "인자 보정 %s: %s",
                        fn,
                        json.dumps(changed, ensure_ascii=False)[:200],
                    )
            else:
                skip = None
            key = call_key(fn, args)
            if not skip and key in done:
                skip = "같은 인자로 이미 불렀다"

            out_of_budget = res.tool_calls_used >= budget
            out_of_time = deadline is not None and deadline.expired(reserve=reserve)
            if skip or out_of_budget or out_of_time:
                why = (
                    skip
                    or ("tool budget exhausted" if out_of_budget else "time budget exhausted")
                )
                res.trace.append(f"{fn}(skipped: {why})")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "content": json.dumps({"error": f"skipped: {why}"}, ensure_ascii=False),
                    }
                )
                continue

            res.tool_calls_used += 1
            done.add(key)
            res.trace.append(f"{fn}({json.dumps(args, ensure_ascii=False)[:120]})")
            try:
                payload = await mcp.call_tool(
                    fn, args, timeout=call_cap(deadline, tool_cap(fn), reserve=reserve)
                )
                _harvest(payload, harvested)
                content = json.dumps(payload, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001 — 한 도구가 죽어도 계속 간다
                log.warning("도구 %s 실패: %s", fn, str(e)[:200])
                content = json.dumps({"error": str(e)[:300]}, ensure_ascii=False)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": content[:TOOL_RESULT_CHARS],
                }
            )

        if finalized:
            break

    # finalize 를 안 부르고 예산을 태운 경우에도 주운 것은 쓴다.
    if not res.items and harvested:
        res.status = "partial" if res.status == "no_evidence" else res.status
        res.items = sorted(harvested.values(), key=lambda e: -len(e.text))[:3]
        if not res.note:
            res.note = "budget exhausted before finalize"
    return res
