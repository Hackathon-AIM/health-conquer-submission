"""L2 2단계 하네스 — 대회 FM(Lunit L2)의 권장 사용법 구현.

★ L2 는 범용 챗 모델이 아니다. 두 단계로 동작한다:

  [검색 단계]  MCP tools + finalize_retrieval 만 주고 근거를 모으게 한다.
               모델은 답을 쓰지 않고 cite_uid 목록을 보고한다.
  [생성 단계]  retrieve_relevant_content 도구 하나만 주고 답을 쓰게 한다.
               모델이 그 도구를 부르면 우리가 검색 단계를 돌려 결과를 넣어준다.

  두 단계의 시스템 프롬프트는 반드시 분리한다 (가이드 명시).

★ 이 파일이 기존 아키텍처에서 대체하는 것
  - L3 (우리가 검색) → 검색 주체가 L2 모델 자신으로 바뀐다. 우리는 도구 서브셋과
    예산만 통제한다. rerank.py 는 L2 경로에서 쓰지 않는다.
  - L4 drafter → generation 단계가 곧 drafter 다. 최종 출력은 반드시 L2 가 생성
    (대회 규칙). 우리 안전 레이어(L1 레드플래그, L4b 게이트)는 그대로 앞뒤를 감싼다.

★ 멀티턴: L2 는 single-turn 최적화다 (가이드 명시).
  → run() 에 들어오기 전에 pipeline 이 질의 재작성(자기완결 질의)을 해서 넘긴다.
  → 생성 단계에는 요약된 이전 대화만 시스템 컨텍스트로 붙인다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Optional

from .config import Config, prompt
from .contracts import Intent
from .llm import LLM, strip_think
from .mcp_client import McpClient, find_cite_uids

# ─────────────────────────────────────────────────────────────
# 상황별 도구 서브셋 — "MCP 가 많은데 어떻게 상황에 맞게 쓰나"의 답
#
# 21개를 전부 주면 모델이 고르다 헤매고, 엉뚱한 코퍼스를 뒤지다 예산을 태운다.
# intent 는 이미 L2 분류 단계(classify)가 정하므로, 매핑은 코드가 한다
# (router.py 와 같은 원칙: 판단은 LLM, 매핑은 코드).
#
# rag_get_all_data_sources / rag_get_data_source_detail 은 개발용(스키마 탐색)이라
# 런타임 서브셋에 넣지 않는다 — 스키마는 우리가 미리 알아내 SQL 프롬프트에 박는다.
# ─────────────────────────────────────────────────────────────
_INDEX = {  # hira(249) + guideline(120) 코퍼스 공용 계층 탐색
    "index_list_documents", "index_get_relevant_nodes",
    "index_get_page_content", "index_keyword_search",
    # index_get_document_structure 는 노드 탐색과 중복이라 기본 제외 (예산 절약)
}
_DRUG = {
    "openapi_mfds_get_drug_indication",      # 허가 효능·용법·주의 (한국) ← 1순위
    "openapi_mfds_check_drug_permission",    # 허가 여부 확인
    "openapi_mfds_find_drugs_by_ingredient", # 동일 성분 대체약
    "adr_retrieve_drug_info",                # DailyMed (영문 라벨: 상호작용·경고)
}
_DRUG_DEEP = {
    "rag_sql_query",                         # FAERS 부작용 통계 (SQL)
    "rag_get_data_source_detail",            # ★ SQL 을 쓰려면 스키마를 볼 수 있어야 한다
    "openapi_hira_get_drug_price",           # 급여 등재·약가
}
_LAW = {"openapi_law_search", "openapi_law_list_articles", "openapi_law_get_article"}
_KCD = {"kcd_search_codes", "kcd_get_name"}
_HIRA = {"hira_updates_search", "openapi_hira_disease_check_code"}
_VECTOR = {"rag_vector_query"}               # pubmed_abstracts + hira_faq

TOOLSETS: dict[Intent, set] = {
    # 응급: 검색 없이 즉시 안내가 원칙 (우리 가설 — ab-emergency 로 측정 대상)
    Intent.EMERGENCY:       set(),
    # "이 약 먹어도 되나요" — 허가사항이 근거의 왕이다
    Intent.DRUG_SAFETY:     _DRUG | _DRUG_DEEP,
    # "뭘 먹어야 하나요" — 약 + 가이드라인(비약물 대처·내원 기준) 둘 다
    Intent.DRUG_RECOMMEND:  _DRUG | _INDEX,
    # 증상 상담 — 가이드라인 코퍼스 중심, 문헌은 보조
    Intent.SYMPTOM_CONSULT: _INDEX | _VECTOR,
    # 정보 질문 — 가이드라인 + 문헌 + 질병코드
    Intent.INFO_REQUEST:    _INDEX | _VECTOR | _KCD,
    # 보험·제도 — HIRA 고시·심의사례 + 법령 체인 + FAQ + 코드
    Intent.POLICY:          _INDEX | _LAW | _HIRA | _VECTOR | _KCD,
}
DEFAULT_TOOLSET = _INDEX | _DRUG | _VECTOR   # intent 를 모를 때(passthrough 등)

# ─────────────────────────────────────────────────────────────
# 우리가 정의해서 주입하는 두 함수 (MCP tool 이 아님 — 가이드 명시)
# ─────────────────────────────────────────────────────────────
FINALIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "finalize_retrieval",
        "description": ("Submit your final citation selection and end the retrieval "
                        "phase. Call this once you have gathered enough evidence, or "
                        "the query needs no retrieval, or the tool budget is exhausted."),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["sufficient", "partial", "no_evidence"]},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cite_uid": {"type": "string"},
                            "relevance_score": {"type": "number"},
                        },
                        "required": ["cite_uid", "relevance_score"],
                    },
                },
                "note": {"type": "string"},
            },
            "required": ["status", "items"],
        },
    },
}

RETRIEVE_TOOL = {
    "type": "function",
    "function": {
        "name": "retrieve_relevant_content",
        "description": ("Retrieve relevant content to ground your answer. "
                        "Pass a single, self-contained query."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

_URL = re.compile(r"https?://\S+")

# ★ 탐색 전용 도구 — 목록·요약만 주고 cite_uid 를 주지 않는다 (실측 확인).
#   여기에 합성 uid 를 붙이면 "문서 제목 목록"을 근거로 인용하게 되므로 붙이지 않는다.
#   근거로 쓰려면 반드시 page_content 등 본문 조회로 이어져야 한다.
EXPLORATORY = {
    "index_list_documents", "index_get_relevant_nodes",
    "index_keyword_search", "index_get_document_structure",
    "openapi_law_search", "openapi_law_list_articles",
    "rag_get_all_data_sources", "rag_get_data_source_detail",
}


def _walk(obj):
    """중첩 JSON 안의 모든 dict 를 순회한다."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _extract_text(d: dict) -> str:
    """도구 결과 dict 하나에서 사람이 읽을 본문을 뽑는다.

    ★ 이것이 우리가 하는 '파싱'이다. 청킹은 서버가 했지만, 도구 결과는 JSON 이라
      그대로 모델에게 주면 (1) 껍데기가 예산을 먹고 (2) 읽기 나쁘다.
      실측 구조:
        index_get_page_content → {cite_uid, url, title, pages:[{page, text}]}
        hira_updates_search    → {items:[{cite_uid, title, gubun, gosi_no, ...}]}
        openapi_* / rag_*      → 도구마다 다름 → 알려진 텍스트 필드를 훑는다
    """
    pages = d.get("pages")
    if isinstance(pages, list) and pages:
        out = []
        for p in pages:
            if isinstance(p, dict) and p.get("text"):
                n = p.get("page")
                out.append(f"[p.{n}] {p['text']}" if n is not None else str(p["text"]))
        if out:
            return "\n\n".join(out)
    for k in ("text", "content", "abstract", "body", "summary", "answer", "value"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    # 알려진 필드가 없으면 스칼라 필드만 key: value 로 (중첩·잡음 제거)
    skip = {"cite_uid", "tool_result_type", "layer", "row_key", "case_id", "source_id",
            "title", "doc_title", "url", "source_url"}   # 이미 머리말에 넣었다
    kv = [f"{k}: {v}" for k, v in d.items()
          if k not in skip and isinstance(v, (str, int, float)) and str(v).strip()]
    return "\n".join(kv)


def render_evidence(uid: str, raw: str, limit: int) -> str:
    """cite_uid 하나에 해당하는 근거 블록 본문을 만든다.

    JSON 파싱에 실패하면 원문을 그대로 자른다 (안전 폴백).
    """
    try:
        obj = json.loads(raw)
    except Exception:
        return raw[:limit]

    target = None
    for d in _walk(obj):
        if d.get("cite_uid") == uid:
            target = d
            break
    if target is None:                       # 합성 uid 등 — 최상위를 쓴다
        target = obj if isinstance(obj, dict) else {"text": raw}

    title = ""
    for k in ("title", "doc_title"):
        if target.get(k):
            title = str(target[k])
            break
    url = target.get("url") or target.get("source_url")
    body = _extract_text(target)

    # ★ 가이드의 예시 블록 형식을 그대로 따른다.
    #     source_type: guideline
    #     url: ...
    #     title: ...
    #     content: ...
    #   L2 는 이 형식으로 학습됐을 가능성이 높다. 임의 형식으로 주면
    #   모델이 인용 번호를 안 붙이거나 근거를 무시할 수 있다
    #   (실측: 근거 4블록을 줬는데 답변에 [1] 이 안 붙었다).
    lines = []
    if url:
        lines.append(f"url: {url}")
    if title:
        lines.append(f"title: {title}")
    lines.append("content: " + body)
    return "\n".join(lines)[:limit]


_EVICTED = "[이전 검색 결과 — 컨텍스트 예산 때문에 생략됨. 이미 본 내용입니다]"


def _evict_old_tool_msgs(msgs: list[dict], budget: int) -> int:
    """오래된 tool 메시지부터 스텁으로 교체해 예산 안으로 되돌린다.

    tool 메시지를 **삭제하면 안 된다** — assistant 의 tool_calls 와 짝이 맞아야
    API 가 400 을 내지 않는다. 그래서 내용만 비운다.
    인용 본문은 cite_store 에 원본이 있으므로 잃는 것이 없다.
    """
    total = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "tool")
    for m in msgs:
        if total <= budget:
            break
        if m.get("role") != "tool" or m.get("content") == _EVICTED:
            continue
        total -= len(m.get("content") or "") - len(_EVICTED)
        m["content"] = _EVICTED
    return max(total, 0)


def _guess_source_type(tool: str, args: Optional[dict] = None,
                       raw: str = "") -> str:
    """가이드 예시의 `source_type: guideline` 처럼 한 단어로 낸다.

    우선순위: 도구 결과가 스스로 밝힌 값 → 호출 인자의 corpus_tag → 도구 이름 추정.
    """
    if raw:
        m = re.search(r'"source_type"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1)
    if tool.startswith("index_"):
        # hira 와 guideline 두 코퍼스를 같은 도구가 쓴다 — 인자로 구분한다
        return (args or {}).get("corpus_tag") or "guideline"
    if "law" in tool:
        return "law"
    if "mfds" in tool:
        return "drug_approval"
    if tool.startswith("adr_"):
        return "drug_label"
    if "hira" in tool:
        return "hira"
    if "kcd" in tool:
        return "kcd"
    if "vector" in tool:
        return (args or {}).get("collection_name") or "literature"
    if tool.startswith("rag_"):
        return "database"
    return tool


class L2Harness:
    def __init__(self, cfg: Config, llm: LLM, mcp: Optional[McpClient] = None):
        self.cfg = cfg
        self.llm = llm
        l2c = cfg.get("l2", {}) or {}
        self.mcp = mcp or McpClient(
            url=l2c.get("mcp_url", "https://mcp.hackathon.lunit.io/mcp"),
            api_key_env=cfg["llm"].get("api_key_env", "LUNIT_FM_API_KEY"),
            timeout=float(l2c.get("tool_timeout", 60.0)),
        )
        self.max_tool_calls = int(l2c.get("max_tool_calls", 8))
        self.max_retrievals = int(l2c.get("max_retrievals_per_turn", 2))
        self.evidence_chars = int(l2c.get("evidence_chars_per_item", 4000))
        self.max_items = int(l2c.get("max_evidence_items", 6))
        self.tool_result_chars = int(l2c.get("tool_result_chars", 3000))
        self.retrieval_budget = int(l2c.get("retrieval_char_budget", 24000))
        self.trace: dict[str, Any] = {}

    # ── 저수준: tool 지원 chat 호출 (llm.chat 은 tools 를 모른다) ──
    async def _chat_tools(self, messages: list[dict], tools: list[dict],
                          tool_choice: str = "auto"):
        lc = self.cfg["llm"]
        kwargs: dict[str, Any] = {
            "model": self.llm.models.get("drafter", "Lunit/L2-preview"),
            "messages": messages,
            "temperature": lc.get("temperature", 0.2),
            # 서버 상한 2048. 넘기면 400 output_limit_exceeded (팀 실측).
            "max_tokens": min(int(lc.get("max_tokens", 2048)), 2048),
        }
        # reasoning 필드가 2048 예산을 먹어 답변이 잘리는 것을 막는다 (llm.py 주석 참조)
        if not lc.get("enable_thinking", False):
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if tools:   # 빈 tools 배열은 일부 서버에서 400 — 아예 빼는 게 안전
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        r = await self.llm.client.chat.completions.create(**kwargs)  # type: ignore[union-attr]
        return r.choices[0].message

    # ── 검색 단계 ───────────────────────────────────────────
    async def retrieval_stage(self, query: str,
                              intent: Optional[Intent] = None) -> dict:
        """MCP 도구 루프를 돌리고 {status, note, blocks:[...]} 를 반환한다."""
        t0 = time.perf_counter()
        allow = TOOLSETS.get(intent, DEFAULT_TOOLSET) if intent else DEFAULT_TOOLSET
        if not allow:                       # 응급 등 — 검색 자체를 하지 않는다
            return {"status": "no_evidence", "note": "retrieval skipped", "blocks": []}

        # MCP 서버에 못 붙으면 검색을 포기하되 예외는 올리지 않는다.
        try:
            await self.mcp.connect()
        except Exception as e:
            self.trace["retrieval"] = {"query": query[:120], "tool_calls": 0,
                                       "status": "no_evidence", "cited": 0,
                                       "error": f"{type(e).__name__}: {e}"}
            return {"status": "no_evidence",
                    "note": f"MCP unreachable ({type(e).__name__})", "blocks": []}
        tools = self.mcp.openai_tools(allow) + [FINALIZE_TOOL]

        msgs: list[dict] = [
            {"role": "system", "content": prompt("l2_retrieval.txt")},
            {"role": "user", "content": query},
        ]
        cite_store: dict[str, dict] = {}    # cite_uid → {tool, text} (원본 전체 보관)
        selection: Optional[dict] = None
        calls = 0
        used_chars = 0                      # 대화에 누적된 도구 결과 총량
        nudged = False                      # "본문을 열어라" 되돌림은 한 번만
        called_tools: list = []             # 실제로 부른 MCP 도구 (E2E 검증용)

        while calls < self.max_tool_calls:
            msg = await self._chat_tools(msgs, tools)
            if not msg.tool_calls:
                # 도구 없이 말로 끝내려 한다 → finalize 를 강제한다
                msgs.append({"role": "assistant", "content": msg.content or ""})
                msgs.append({"role": "user",
                             "content": "finalize_retrieval 을 호출해 검색을 종료하세요."})
                msg = await self._chat_tools(msgs, [FINALIZE_TOOL],
                                             tool_choice="required")
                if not msg.tool_calls:
                    break

            msgs.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })

            done = False
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}

                if name == "finalize_retrieval":
                    selection = args
                    msgs.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": "retrieval finalized"})
                    done = True
                    continue

                calls += 1
                called_tools.append(name)
                result = await self.mcp.call(name, args)
                uids = find_cite_uids(result)
                # ★ 실측(probe 2차): 조회 도구는 cite_uid 를 준다(MFDS·KCD·DailyMed·
                #   약가·벡터검색 모두 확인). 안 주는 것은 탐색 도구뿐이고, 그건 설계상
                #   맞다 — 목록·요약은 근거가 아니다.
                #   그래서 합성 uid 는 '탐색 도구가 아닌데 uid 가 없는' 경우에만 붙인다.
                if (not uids and name not in EXPLORATORY
                        and not result.startswith("[tool_error]")):
                    synth = "cite-" + hashlib.md5(
                        (name + json.dumps(args, ensure_ascii=False, sort_keys=True)
                         + result[:200]).encode("utf-8")).hexdigest()[:16]
                    uids = [synth]
                    result += f"\n\ncite_uid: {synth}"
                for uid in uids:
                    # 원문 전체를 보관한다 — 자르는 것은 블록을 만들 때 한 번만.
                    # 여기서 미리 자르면 뒤쪽 페이지 본문이 영영 사라진다.
                    cite_store.setdefault(uid, {"tool": name, "args": args, "text": result})
                budget_note = ""
                if calls >= self.max_tool_calls:
                    budget_note = ("\n\n[예산 소진] 더 이상 도구를 호출할 수 없습니다. "
                                   "지금 즉시 finalize_retrieval 을 호출하세요.")
                shown = result[: self.tool_result_chars]
                if len(result) > len(shown):
                    shown += (f"\n…[{len(result) - len(shown):,}자 생략 — 인용 시에는 "
                              f"원문 전체가 사용됩니다]")
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": shown + budget_note})
                used_chars += len(shown)
                if used_chars > self.retrieval_budget:
                    # 오래된 도구 결과부터 잘라낸다 — 최근 것이 판단에 더 쓸모 있고,
                    # 인용 본문은 cite_store 에 원본이 있으므로 잃는 것이 없다.
                    used_chars = _evict_old_tool_msgs(msgs, self.retrieval_budget)
            # ★ 탐색만 하고 끝내려는 경우를 막는다 (실측 실패 모드).
            #   모델이 목록·노드만 보고 finalize 하면 cite_uid 가 하나도 없어
            #   생성 단계가 빈손이 된다. 예산이 남아 있으면 한 번 되돌려 보낸다.
            if done and not cite_store and calls < self.max_tool_calls and not nudged:
                nudged = True
                msgs.append({
                    "role": "user",
                    "content": ("아직 인용 가능한 근거가 없습니다. 탐색 도구 결과에는 "
                                "cite_uid 가 없기 때문입니다.\n"
                                "찾은 doc_id 와 page range 로 index_get_page_content 를 "
                                "호출하거나(가이드라인·HIRA), 해당 조회 도구로 본문을 "
                                "여세요. 그다음 finalize_retrieval 을 다시 호출하세요."),
                })
                selection, done = None, False
                continue
            if done:
                break

        # ★ 실측(1차 스모크)에서 드러난 실패 모드 두 가지를 여기서 막는다.
        #
        #   (a) 모델이 finalize 를 아예 안 부름
        #   (b) ★ 모델이 근거를 찾고도 items=[] 로 partial 을 냄
        #       실제 사례: "tyfna.1.6.3.6(페이지 50)에 목표 혈압 130 미만이 있다.
        #       그러나 본문 CKD 섹션을 아직 못 찾았다" → status=partial, items=[]
        #       완벽주의 때문에 찾은 근거를 통째로 버린다. 생성 단계는 빈손이 된다.
        #
        # 둘 다 "수집한 uid 를 그대로 쓴다"로 복구한다. note 는 살려서 넘긴다
        # (모델이 무엇을 못 찾았는지가 생성 단계에 유용한 정보다).
        if selection is None:
            selection = {"status": "partial" if cite_store else "no_evidence",
                         "items": [], "note": "model did not call finalize_retrieval"}
        if not (selection.get("items") or []) and cite_store:
            selection["items"] = [{"cite_uid": u, "relevance_score": 0.5}
                                  for u in cite_store]
            selection["note"] = (selection.get("note", "") +
                                 " [harness: 모델이 빈 items 를 냈으므로 수집분으로 복구]").strip()

        items = sorted(selection.get("items") or [],
                       key=lambda x: -float(x.get("relevance_score", 0)))[: self.max_items]
        blocks = []
        for i, it in enumerate(items, 1):
            uid = it.get("cite_uid", "")
            src = cite_store.get(uid)
            if not src:
                continue        # 모델이 지어낸 uid 는 버린다
            body = render_evidence(uid, src["text"], self.evidence_chars)
            blocks.append(
                f"[{i}]\nsource_type: "
                + _guess_source_type(src["tool"], src.get("args"), src["text"])
                + f"\n{body}"
            )

        self.trace["retrieval"] = {
            "query": query[:120], "tool_calls": calls,
            "status": selection.get("status"),
            "cited": len(blocks), "collected_uids": len(cite_store),
            # ★ 어떤 MCP 도구가 실제로 불렸고 어떤 코퍼스가 인용됐는지.
            #   TOOLSETS 매핑이 의도대로 작동하는지 E2E 로 확인하는 근거다.
            "tools": called_tools,
            "sources": sorted({b.split("source_type: ")[1].split("\n")[0]
                               for b in blocks if "source_type: " in b}),
            "ms": int((time.perf_counter() - t0) * 1000),
        }
        return {"status": selection.get("status", "partial"),
                "note": selection.get("note", ""), "blocks": blocks}

    # ── 근거 강제 판정 ──────────────────────────────────────
    #
    # ★ 이 해커톤에서 MCP 는 도구가 아니라 **RAG 그 자체**다.
    #   가이드라인 120편·HIRA 249편·법령·허가사항·PubMed·FAERS 가 전부 여기 있고,
    #   다른 접근 경로가 없다. 그런데 L2 는 "알 것 같으면" 그냥 답해버린다.
    #   E2E 실측: 검색 0회 · 인용 0건인데 답변엔 [1] 이 붙어 있었다(환각).
    #   → 출처가 필요한 질문은 하네스가 검색을 먼저 돌리고 근거를 쥐여준다.
    _NEEDS_EVIDENCE = re.compile(
        r"(가이드라인|지침|권고|기준치|목표(치|가|는)|"                    # 진료지침
        r"급여|본인부담|비급여|고시|심의|수가|약가|상한|청구|"              # 보험·제도
        r"법(률|령|상|에|은|이)|조문|제\s*\d+\s*조|시행령|시행규칙|"        # 법령
        r"허가|효능|효과|용법|용량|복용량|금기|병용|상호작용|부작용|"        # 약물
        r"같이\s*(먹|복용|드)|함께\s*(먹|복용|드)|동시에\s*(먹|복용)|"        # 병용 구어체
        r"먹어도\s*(되|괜찮)|복용해도\s*(되|괜찮)|"
        r"KCD|질병\s*코드|상병|분류코드|"                                  # 코드
        r"최신|연구|논문|근거|공식|정설|통계|유병률)")                      # 문헌

    _EVIDENCE_INTENTS = {Intent.POLICY, Intent.DRUG_SAFETY, Intent.INFO_REQUEST}

    def _needs_evidence(self, query: str, intent: Optional[Intent]) -> bool:
        if intent in self._EVIDENCE_INTENTS:
            return True
        return bool(self._NEEDS_EVIDENCE.search(query))

    async def _safe_retrieval(self, query: str, intent: Optional[Intent]) -> dict:
        """예외를 올리지 않는 검색 — 실패해도 답변은 나와야 한다."""
        try:
            return await self.retrieval_stage(query, intent)
        except Exception as e:
            self.trace["retrieval_error"] = f"{type(e).__name__}: {e}"
            return {"status": "no_evidence",
                    "note": f"retrieval unavailable ({type(e).__name__})",
                    "blocks": []}

    _CITE_MARK = re.compile(r"\s*\[\d+\]")

    def _finish(self, text: str, evidence_given: bool) -> str:
        """근거를 안 줬는데 인용 번호가 붙어 있으면 떼어낸다.

        지어낸 인용은 없는 것보다 나쁘다 — 정확성이 채점의 43% 다.
        """
        out = strip_think(text or "")
        if not evidence_given and self._CITE_MARK.search(out):
            self.trace["stripped_fake_citations"] = len(self._CITE_MARK.findall(out))
            out = self._CITE_MARK.sub("", out)
        return out

    # ── 생성 단계 ───────────────────────────────────────────
    async def generate(self, user_query: str, *,
                       intent: Optional[Intent] = None,
                       context_note: str = "",
                       instructions: str = "") -> str:
        """생성 단계 실행 — retrieve_relevant_content 하나만 노출한다.

        user_query    자기완결 질의 (멀티턴 재작성은 pipeline 책임)
        context_note  세션 요약 — 나이·임신·위험인자·복용약
        instructions  ★ 응답 설계 지시 — 깊이·되묻기·불확실성·상대 수준.
                      분류 단계가 정한 것을 여기서 실제 답변에 반영시킨다.
                      이게 없으면 분류 결과가 계산만 되고 버려진다.
        """
        self.trace = {}
        sys = prompt("l2_generation.txt")
        if context_note:
            sys += f"\n\n[대화 맥락 요약 — 답변에 반영하세요]\n{context_note}"
        if instructions:
            sys += f"\n\n[이번 답변 지시 — 반드시 지키세요]\n{instructions}"

        msgs: list[dict] = [{"role": "system", "content": sys},
                            {"role": "user", "content": user_query}]
        retrievals = 0
        evidence_given = False       # 근거를 실제로 건넸는가 (인용 허용 여부)

        # ★ 출처가 필요한 질문인데 모델이 도구를 안 부르는 문제 (E2E 실측).
        #   "가이드라인상 CKD 혈압 목표"를 물었는데 검색 0회로 답하면서 [1] 을
        #   지어냈다. 프롬프트만으로는 못 막는다 — 첫 호출을 하네스가 강제한다.
        #   가이드도 "retrieval 과 generation 을 잇는 방식은 자유"라고 했다.
        forced_ok = False
        if intent is not Intent.EMERGENCY and self._needs_evidence(user_query, intent):
            r0 = await self._safe_retrieval(user_query, intent)
            self.trace["forced_retrieval"] = True
            if r0["blocks"]:
                evidence_given = forced_ok = True
                msgs[0]["content"] += (
                    "\n\n[미리 확보한 근거 — 이 번호로만 인용하세요]\n"
                    + f"status: {r0['status']}\n"
                    + (f"note: {r0['note']}\n" if r0["note"] else "")
                    + "\n\n".join(r0["blocks"])
                )
            else:
                msgs[0]["content"] += (
                    f"\n\n[근거 검색 결과] status: {r0['status']} — 인용 가능한 근거를 "
                    "확보하지 못했습니다. 인용 번호를 쓰지 말고, 일반적인 의학 지식에 "
                    "근거한 답임을 밝히세요."
                )

        # ★ 강제 검색으로 이미 근거를 쥐여줬으면 도구를 다시 열어주지 않는다.
        #   E2E 실측: 열어두면 모델이 한 번 더 검색해서 같은 턴에 검색이 2회 돈다.
        #     ⑥ 법령 질문 89.2초 (강제 1회 + 모델 1회, 두 번째는 소득 0)
        #     ③ 병용금기 88.8초 (adr_retrieve_drug_info 를 3번 반복 호출)
        #   근거는 이미 시스템 프롬프트에 있으므로 바로 답을 쓰게 한다 — 절반이 준다.
        if forced_ok:
            msg = await self._chat_tools(msgs, [RETRIEVE_TOOL], tool_choice="none")
            out = self._finish(msg.content or "", evidence_given)
            if out:
                return out
            # 빈 응답이면 아래 일반 루프로 폴백

        for _ in range(self.max_retrievals + 1):
            msg = await self._chat_tools(msgs, [RETRIEVE_TOOL])
            if not msg.tool_calls:
                return self._finish(msg.content or "", evidence_given)

            msgs.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    q = json.loads(tc.function.arguments or "{}").get("query", user_query)
                except Exception:
                    q = user_query
                if retrievals < self.max_retrievals:
                    retrievals += 1
                    # ★ 검색이 실패해도 답변은 나와야 한다.
                    #   MCP 가 막혀 있거나(격리 환경) 도구가 죽어도 여기서 예외가
                    #   위로 올라가면 serve.py 가 사과 문구로 대체해 버린다.
                    #   그러면 그 문항은 사실상 0점이다.
                    #   근거 없이라도 L2 가 자기 지식으로 답하게 두는 편이 훨씬 낫다.
                    r = await self._safe_retrieval(q, intent)
                    if r["blocks"]:
                        evidence_given = True
                    body = f"status: {r['status']}\n"
                    if r["note"]:
                        body += f"note: {r['note']}\n"
                    body += "\n\n".join(r["blocks"]) if r["blocks"] else "(no evidence found)"
                else:
                    body = ("status: no_evidence\nnote: retrieval budget exhausted — "
                            "answer with what you have, stating uncertainty")
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": body})

        # 루프 한도 초과 — 마지막으로 답만 받는다.
        # ⚠️ tools 를 통째로 빼면 L2 가 빈 content 를 낼 수 있어(1차 스모크 #2),
        #    도구는 남기고 tool_choice="none" 으로 호출만 막는다.
        msgs.append({"role": "user",
                     "content": "지금까지의 정보로 최종 답변을 작성하세요. 도구를 더 부르지 마세요."})
        msg = await self._chat_tools(msgs, [RETRIEVE_TOOL], tool_choice="none")
        out = self._finish(msg.content or "", evidence_given)
        if not out:   # 그래도 비면 마지막 assistant 발화라도 살린다
            for m in reversed(msgs):
                if m.get("role") == "assistant" and (m.get("content") or "").strip():
                    return self._finish(m["content"], evidence_given)
        return out
