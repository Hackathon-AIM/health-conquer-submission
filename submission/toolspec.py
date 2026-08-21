"""툴 정의(menu tax) 를 예산 안에 밀어 넣는 계층.

MCP 서버가 주는 툴은 21개다. 정의를 그대로 실으면 스키마만으로 수천 토큰이라
2~3K 창에서는 사용자 질문이 들어갈 자리가 남지 않는다. 결과를 아무리 잘 줄여도
메뉴판에서 먼저 죽는다.

두 단계로 줄인다.
  1. 선택 — 질문의 표면 신호로 후보 툴을 점수순 정렬한다. 이건 RAG-MCP 가 임베딩으로
     하는 일을 규칙으로 하는 것이다. 툴 21개짜리 고정 카탈로그에서는 규칙이 임베딩보다
     싸고, 틀렸을 때 왜 틀렸는지 읽을 수 있다.
  2. 축약 — 예산이 허락하는 만큼만 담되, 담을 때마다 description·enum·optional 인자를
     깎는다. 툴 3개를 온전히 싣는 것보다 5개를 뼈대만 싣는 편이 라우팅에 유리하다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .context import TokenCounter

# 실제 Lunit MCP 카탈로그 기준. 이름이 서버와 어긋나면 그 툴은 조용히 후보에서 빠지므로
# (allowlist 교집합), 추측으로 채우지 말고 tools/list 결과와 맞춘다.
_TOPICS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "drug",
        ("약", "약물", "복용", "처방", "투약", "성분", "제품명", "정제", "주사", "부작용", "이상반응",
         "상호작용", "병용", "금기", "용량", "용법", "적응증", "허가", "drug", "dose", "dosage",
         "interaction", "contraindication", "adverse", "label", "indication", "ingredient"),
        ("adr_retrieve_drug_info", "openapi_mfds_get_drug_indication",
         "openapi_mfds_check_drug_permission", "openapi_mfds_find_drugs_by_ingredient"),
    ),
    (
        "safety_stats",
        ("이상사례", "부작용 통계", "faers", "보고 건수", "signal", "빈도"),
        ("rag_sql_query", "rag_get_data_source_detail"),
    ),
    (
        "reimbursement",
        ("급여", "비급여", "보험", "약가", "상한가", "심평원", "hira", "고시", "심의", "청구", "삭감",
         "본인부담", "산정특례"),
        ("hira_updates_search", "openapi_hira_get_drug_price", "index_get_relevant_nodes",
         "index_keyword_search"),
    ),
    (
        "law",
        ("법", "법령", "법률", "조문", "시행령", "시행규칙", "의료법", "약사법", "law", "statute",
         "regulation", "article"),
        ("openapi_law_search", "openapi_law_list_articles", "openapi_law_get_article"),
    ),
    (
        "code",
        ("kcd", "질병코드", "상병", "진단코드", "코드", "icd", "청구코드"),
        ("kcd_search_codes", "kcd_get_name", "openapi_hira_disease_check_code"),
    ),
    (
        "evidence",
        ("연구", "논문", "근거", "가이드라인", "지침", "권고", "study", "trial", "guideline",
         "evidence", "recommendation", "pubmed", "메타분석", "코호트"),
        ("rag_vector_query", "index_get_relevant_nodes", "index_keyword_search",
         "index_get_page_content"),
    ),
)

# 어느 주제로도 안 걸릴 때의 기본값. 가이드라인·HIRA 문서 인덱스가 가장 넓게 덮는다.
_DEFAULT_TOOLS = (
    "index_get_relevant_nodes",
    "rag_vector_query",
    "index_keyword_search",
)

# 이 툴들은 '어떤 데이터가 있는지 둘러보는' 용도다. 창이 좁으면 한 턴을 통째로
# 낭비하므로 명시적으로 요청되지 않는 한 싣지 않는다.
_DISCOVERY_TOOLS = {"rag_get_all_data_sources", "rag_get_data_source_detail",
                    "index_list_documents", "index_get_document_structure"}

_SENTENCE_END = re.compile(r"(?<=[.!?。])\s")


def rank_tool_names(query: str) -> list[str]:
    """질문 표면 신호로 툴 이름을 점수순 정렬한다."""
    lowered = query.lower()
    scores: dict[str, float] = {}
    for _topic, keywords, tools in _TOPICS:
        hits = sum(1 for keyword in keywords if keyword in lowered)
        if not hits:
            continue
        for rank, name in enumerate(tools):
            # 주제 적중 수가 1순위, 주제 안에서의 대표성이 2순위.
            scores[name] = max(scores.get(name, 0.0), hits + (len(tools) - rank) * 0.01)
    for rank, name in enumerate(_DEFAULT_TOOLS):
        scores.setdefault(name, 0.001 * (len(_DEFAULT_TOOLS) - rank))
    return [name for name, _ in sorted(scores.items(), key=lambda item: -item[1])]


def _shorten(text: str, counter: TokenCounter, max_tokens: int) -> str:
    """설명은 첫 문장이 거의 전부다. 문장 경계에서 끊고 모자라면 토큰으로 자른다."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    first = _SENTENCE_END.split(text, 1)[0]
    if counter.estimate_text(first) <= max_tokens:
        return first
    return counter.truncate(first, max_tokens)


def _slim_schema(schema: Any, counter: TokenCounter, *, level: int) -> Any:
    """inputSchema 에서 모델이 인자를 맞히는 데 필요 없는 것들을 걷어낸다.

    level 0 = 온전 / 1 = 설명 축약 / 2 = required 만 + 설명 제거.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    required = [str(name) for name in schema.get("required") or [] if isinstance(name, str)]
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {"type": "object", "properties": {}, **({"required": required} if required else {})}

    slim_properties: dict[str, Any] = {}
    for name, spec in properties.items():
        if level >= 2 and name not in required:
            continue
        if not isinstance(spec, dict):
            slim_properties[name] = {"type": "string"}
            continue
        entry: dict[str, Any] = {}
        if isinstance(spec.get("type"), str):
            entry["type"] = spec["type"]
        elif spec.get("anyOf") or spec.get("oneOf"):
            entry["type"] = "string"
        enum = spec.get("enum")
        if isinstance(enum, list) and 0 < len(enum) <= 8:
            entry["enum"] = enum[:8]
        if level == 0 and isinstance(spec.get("items"), dict) and isinstance(spec["items"].get("type"), str):
            entry["items"] = {"type": spec["items"]["type"]}
        elif entry.get("type") == "array":
            entry["items"] = {"type": "string"}
        description = spec.get("description")
        if level <= 1 and isinstance(description, str) and description.strip():
            entry["description"] = _shorten(description, counter, 24 if level else 40)
        slim_properties[name] = entry or {"type": "string"}

    output: dict[str, Any] = {"type": "object", "properties": slim_properties}
    if required:
        output["required"] = required
    return output


def minify(definition: dict[str, Any], counter: TokenCounter, *, level: int) -> dict[str, Any]:
    """MCP 툴 정의 하나를 OpenAI function 스펙으로, level 만큼 깎아서 변환한다."""
    name = str(definition.get("name") or "")
    description = str(definition.get("description") or "")
    if level >= 2:
        description = _shorten(description, counter, 12)
    elif level == 1:
        description = _shorten(description, counter, 28)
    else:
        description = _shorten(description, counter, 60)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _slim_schema(definition.get("inputSchema"), counter, level=level),
        },
    }


def _cost(tool: dict[str, Any], counter: TokenCounter) -> int:
    return counter.estimate_text(
        json.dumps(tool, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def build_tools(
    definitions: list[dict[str, Any]],
    query: str,
    counter: TokenCounter,
    *,
    token_budget: int,
    always_include: list[dict[str, Any]] | None = None,
    max_tools: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """예산 안에 들어가는 tools 배열을 만든다. (tools, 진단정보) 반환.

    always_include (finalize 같은 우리 툴)는 예산에서 먼저 떼어 놓는다. 이게 없으면
    모델이 검색을 끝낼 방법이 사라진다.
    """
    always = list(always_include or [])
    remaining = token_budget - sum(_cost(tool, counter) for tool in always)
    by_name = {str(d.get("name") or ""): d for d in definitions if isinstance(d, dict) and d.get("name")}
    ordered = [name for name in rank_tool_names(query) if name in by_name]
    lowered = query.lower()
    if not any(word in lowered for word in ("어떤 데이터", "데이터 소스", "목록", "list")):
        ordered = [name for name in ordered if name not in _DISCOVERY_TOOLS]
    if not ordered:
        ordered = [name for name in by_name if name not in _DISCOVERY_TOOLS][:max_tools]

    for level in (0, 1, 2):
        selected: list[dict[str, Any]] = []
        used = 0
        for name in ordered:
            if len(selected) >= max_tools:
                break
            candidate = minify(by_name[name], counter, level=level)
            cost = _cost(candidate, counter)
            if used + cost > remaining:
                continue
            selected.append(candidate)
            used += cost
        if selected:
            return always + selected, {
                "level": level,
                "selected": [t["function"]["name"] for t in selected],
                "tool_tokens": used + (token_budget - remaining),
                "budget": token_budget,
                "considered": len(ordered),
            }

    # 어떤 수준으로 깎아도 한 개도 못 넣는다 = 예산 배분이 잘못됐다는 신호.
    return always, {
        "level": 2,
        "selected": [],
        "tool_tokens": token_budget - remaining,
        "budget": token_budget,
        "considered": len(ordered),
        "starved": True,
    }
