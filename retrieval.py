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
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("retrieval")

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

    def as_prompt(self, limit: int = 1400) -> str:
        """generation 단계에 넘길 형태. 대시보드 예시의 모양을 따른다."""
        lines = [f"status: {self.status}"]
        if self.note:
            lines.append(f"note: {self.note}")
        for i, ev in enumerate(self.items, 1):
            lines.append("")
            lines.append(f"[{i}]")
            if ev.source_type:
                lines.append(f"source_type: {ev.source_type}")
            if ev.url:
                lines.append(f"url: {ev.url}")
            if ev.title:
                lines.append(f"title: {ev.title}")
            lines.append(f"content: {ev.text[:limit]}")
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
        mcp_tools = await mcp.list_tools()
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

    for _ in range(budget):
        if deadline is not None and deadline.expired(reserve=reserve):
            log.info("시간 예산으로 retrieval 조기 종료 (남은 %.0fs)", deadline.remaining())
            res.note = (res.note + " " if res.note else "") + "retrieval cut short by time budget"
            break
        try:
            data = await call_fm(messages, step_tokens, {"tools": tools})
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

            res.tool_calls_used += 1
            step = f"{fn}({json.dumps(args, ensure_ascii=False)[:120]})"
            try:
                payload = await mcp.call_tool(fn, args)
                before = len(harvested)
                _harvest(payload, harvested)
                content = json.dumps(payload, ensure_ascii=False)
                # 호출만 찍으면 "빈손으로 돌아온 것"과 "받고도 안 연 것"이 구분되지 않는다.
                # 응답 크기와 새로 얻은 cite_uid 수를 같이 남긴다.
                step += f" → {len(content):,}자 · cite_uid +{len(harvested) - before}"
            except Exception as e:  # noqa: BLE001 — 한 도구가 죽어도 계속 간다
                log.warning("도구 %s 실패: %s", fn, str(e)[:200])
                content = json.dumps({"error": str(e)[:300]}, ensure_ascii=False)
                step += f" → 실패: {str(e)[:80]}"
            res.trace.append(step)

            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id") or "", "content": content[:12000]}
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
