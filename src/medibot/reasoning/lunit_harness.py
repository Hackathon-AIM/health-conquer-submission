import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx

from medibot.config.settings import Settings
from medibot.core.prompts import PromptLoader
from medibot.core.schemas import (
    ClinicalState,
    QueryAnalysis,
    ResponseRequirements,
    RetrievalEvidence,
)
from medibot.sources.mcp_client import LunitMCPClient


class LunitL2Harness:
    """Two-phase Lunit L2 harness using MCP tools for retrieval."""

    EVIDENCE_PRODUCING_TOOLS = {
        "index_get_page_content",
        "hira_updates_search",
        "rag_vector_query",
        "adr_retrieve_drug_info",
        "openapi_law_get_article",
        "openapi_mfds_get_drug_indication",
    }
    CITE_UID_PATTERN = re.compile(r"\bcite_uid\s*[:=]\s*([A-Za-z0-9_.:/#-]+)")

    def __init__(
        self,
        settings: Settings,
        mcp_client: LunitMCPClient | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.prompt_loader = PromptLoader(settings.prompt_dir)
        self.answer_prompt = self.prompt_loader.load("answer_generator.md")
        self.mcp_client = mcp_client or LunitMCPClient(settings)
        self._http_client = http_client
        self.last_evidence: list[RetrievalEvidence] = []
        self.last_trace: dict[str, Any] = {}

    def generation_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "retrieve_relevant_content",
                    "description": (
                        "Retrieve Lunit MCP-grounded medical evidence. Pass one "
                        "self-contained query."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Self-contained medical retrieval query.",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    async def retrieval_tools(self, query: str | None = None) -> list[dict[str, Any]]:
        tools = []
        for tool in self._select_mcp_tools(await self.mcp_client.list_tools(), query or ""):
            name = str(tool.get("name", ""))
            if not name:
                continue
            input_schema = tool.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": self._compact_description(tool),
                        "parameters": input_schema,
                    },
                }
            )
        tools.append(self.finalize_retrieval_tool())
        return tools

    def _select_mcp_tools(
        self, tools: list[dict[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        lowered = query.lower()
        prioritized_names: list[str] = []
        if any(
            term in lowered
            for term in ["pubmed", "abstract", "trial", "study", "literature", "논문"]
        ):
            prioritized_names.append("rag_vector_query")
        if any(term in lowered for term in ["law", "법", "조문", "statute"]):
            prioritized_names.extend(
                [
                    "openapi_law_search",
                    "openapi_law_list_articles",
                    "openapi_law_get_article",
                ]
            )
        if any(
            term in lowered
            for term in [
                "drug",
                "medicine",
                "dose",
                "dosage",
                "interaction",
                "ibuprofen",
                "이부프로펜",
                "약",
                "용량",
                "상호작용",
            ]
        ):
            prioritized_names.extend(
                [
                    "adr_retrieve_drug_info",
                    "openapi_mfds_get_drug_indication",
                    "openapi_mfds_check_drug_permission",
                    "openapi_mfds_find_drugs_by_ingredient",
                ]
            )
        if any(term in lowered for term in ["hira", "급여", "보험", "심평원", "coverage"]):
            prioritized_names.extend(
                [
                    "index_get_relevant_nodes",
                    "index_keyword_search",
                    "index_get_page_content",
                    "rag_vector_query",
                    "openapi_hira_get_drug_price",
                ]
            )
        if any(
            term in lowered
            for term in [
                "guideline",
                "가이드라인",
                "권고",
                "blood pressure",
                "hypertension",
                "ckd",
                "chronic kidney",
                "만성 신장",
                "고혈압",
            ]
        ):
            prioritized_names.extend(
                [
                    "index_list_documents",
                    "index_get_relevant_nodes",
                    "index_keyword_search",
                    "index_get_page_content",
                ]
            )
        if any(term in lowered for term in ["notice", "updates", "고시", "심의사례"]):
            prioritized_names.extend(
                [
                    "hira_updates_search",
                ]
            )
        if any(
            term in lowered
            for term in ["latest", "evidence", "최신", "근거"]
        ):
            prioritized_names.append("rag_vector_query")
        if any(term in lowered for term in ["faers", "adverse", "부작용", "이상사례"]):
            prioritized_names.extend(
                ["rag_sql_query", "rag_get_data_source_detail", "adr_retrieve_drug_info"]
            )
        if any(term in lowered for term in ["kcd", "icd", "code", "질병코드", "진단코드"]):
            prioritized_names.extend(
                [
                    "kcd_search_codes",
                    "kcd_get_name",
                    "openapi_hira_disease_check_code",
                ]
            )
        if any(term in lowered for term in ["price", "pricing", "약가", "상한가"]):
            prioritized_names.append("openapi_hira_get_drug_price")
        if not prioritized_names:
            prioritized_names.extend(
                [
                    "rag_get_all_data_sources",
                    "rag_vector_query",
                    "index_get_relevant_nodes",
                    "index_get_page_content",
                    "adr_retrieve_drug_info",
                ]
            )

        tools_by_name = {tool.get("name"): tool for tool in tools}
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in prioritized_names:
            if name in seen or name not in tools_by_name:
                continue
            selected.append(tools_by_name[name])
            seen.add(name)
            if len(selected) >= 5:
                break
        return selected

    def _compact_description(self, tool: dict[str, Any]) -> str:
        description = str(tool.get("description") or tool.get("title") or "")
        description = " ".join(description.split())
        if len(description) > 240:
            return description[:237] + "..."
        return description

    def finalize_retrieval_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "finalize_retrieval",
                "description": "Submit final citation selection and end retrieval.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["sufficient", "partial", "no_evidence"],
                        },
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

    async def generate(
        self,
        message: str,
        state: ClinicalState,
        analysis: QueryAnalysis,
        requirements: ResponseRequirements,
        seed_evidence: list[RetrievalEvidence],
    ) -> str:
        self.last_evidence = []
        self.last_trace = {
            "l2_retrieval_phase_status": None,
            "l2_mcp_tool_call_count": 0,
            "l2_selected_cite_uids": [],
            "l2_finalize_note": None,
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._generation_system_prompt()},
            {
                "role": "user",
                "content": self._build_generation_context(
                    message, state, analysis, requirements, seed_evidence
                ),
            },
        ]

        for _ in range(self.settings.l2_generation_tool_budget + 1):
            data = await self._chat_completion(messages, self.generation_tools())
            assistant_message = dict(data["choices"][0]["message"])
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                return str(assistant_message.get("content") or "")
            messages.append(assistant_message)
            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name")
                arguments = self._parse_arguments(
                    tool_call.get("function", {}).get("arguments")
                )
                if name != "retrieve_relevant_content":
                    content = f"Unsupported generation tool: {name}"
                else:
                    content = await self.retrieve_relevant_content(
                        str(arguments.get("query") or message)
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": content,
                    }
                )
        raise RuntimeError("Lunit L2 generation tool budget exhausted.")

    async def retrieve_relevant_content(self, query: str) -> str:
        selection, evidence = await self._run_retrieval_phase(query)
        self.last_evidence = self._dedupe_evidence(
            [*self.last_evidence, *evidence]
        )[: self.settings.evidence_limit]
        self.last_trace["l2_retrieval_phase_status"] = selection.get("status")
        self.last_trace["l2_selected_cite_uids"] = [
            item.cite_uid for item in self.last_evidence if item.cite_uid
        ]
        self.last_trace["l2_finalize_note"] = selection.get("note")
        return self._format_evidence_for_generation(
            str(selection.get("status") or "no_evidence"), self.last_evidence
        )

    def _dedupe_evidence(
        self, evidence: list[RetrievalEvidence]
    ) -> list[RetrievalEvidence]:
        seen: set[str] = set()
        unique: list[RetrievalEvidence] = []
        for item in evidence:
            key = item.cite_uid or item.id
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    async def _run_retrieval_phase(
        self, query: str
    ) -> tuple[dict[str, Any], list[RetrievalEvidence]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._retrieval_system_prompt()},
            {"role": "user", "content": query},
        ]
        tool_results: list[dict[str, Any]] = []
        retrieval_tools = await self.retrieval_tools(query)

        for _ in range(self.settings.l2_retrieval_tool_budget + 1):
            if (
                int(self.last_trace.get("l2_mcp_tool_call_count") or 0)
                >= self.settings.l2_retrieval_tool_budget
            ):
                break
            data = await self._chat_completion(messages, retrieval_tools)
            assistant_message = dict(data["choices"][0]["message"])
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                break
            messages.append(assistant_message)
            for tool_call in tool_calls:
                if (
                    int(self.last_trace.get("l2_mcp_tool_call_count") or 0)
                    >= self.settings.l2_retrieval_tool_budget
                ):
                    break
                function = tool_call.get("function", {})
                name = function.get("name")
                arguments = self._parse_arguments(function.get("arguments"))
                if name == "finalize_retrieval":
                    selection = self._normalize_selection(arguments)
                    evidence = self._evidence_from_selection(selection, tool_results)
                    return selection, evidence

                result = await self.mcp_client.call_tool(str(name), arguments)
                self.last_trace["l2_mcp_tool_call_count"] += 1
                tool_results.append({"tool_name": name, "arguments": arguments, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": self._retrieval_tool_response_text(result),
                    }
                )
        selection = self._selection_from_budget_exhaustion(tool_results)
        evidence = self._evidence_from_selection(selection, tool_results)
        if not evidence:
            evidence = self._evidence_from_tool_results(tool_results)
        return selection, evidence

    def _selection_from_budget_exhaustion(
        self, tool_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        cite_uids: list[str] = []
        for tool_result in tool_results:
            for cite_uid in self._extract_cite_uids(tool_result.get("result")):
                if cite_uid not in cite_uids:
                    cite_uids.append(cite_uid)
        if cite_uids:
            return {
                "status": "partial",
                "items": [
                    {"cite_uid": cite_uid, "relevance_score": 0.5}
                    for cite_uid in cite_uids[: self.settings.evidence_limit]
                ],
                "note": "retrieval ended by harness after tool budget or missing finalize_retrieval",
            }
        return {
            "status": "partial" if tool_results else "no_evidence",
            "items": [],
            "note": "retrieval ended by harness after tool budget or missing finalize_retrieval",
        }

    def _evidence_from_tool_results(
        self, tool_results: list[dict[str, Any]]
    ) -> list[RetrievalEvidence]:
        evidence: list[RetrievalEvidence] = []
        evidence_results = [
            tool_result
            for tool_result in tool_results
            if self._is_evidence_producing_tool(str(tool_result.get("tool_name") or ""))
        ]
        for tool_result in evidence_results[-self.settings.evidence_limit :]:
            raw_result = tool_result.get("result")
            cite_uid = next(iter(self._extract_cite_uids(raw_result)), None)
            content = self._tool_result_text(
                raw_result, limit=self.settings.l2_tool_result_char_limit
            )
            if not content or content in {"{}", "[]", "null"}:
                continue
            evidence.append(
                RetrievalEvidence(
                    id=cite_uid or f"lunit-mcp-{uuid4()}",
                    source="lunit_mcp",
                    title=self._first_string(raw_result, ["title", "name", "source"])
                    or cite_uid
                    or str(tool_result.get("tool_name") or "Lunit MCP evidence"),
                    content=content,
                    cite_uid=cite_uid,
                    source_url=self._first_string(raw_result, ["url", "source_url", "link"]),
                    corpus_tag=self._first_string(raw_result, ["corpus_tag", "source_type"]),
                    page_range=self._page_range(raw_result),
                    raw_tool_name=str(tool_result.get("tool_name") or ""),
                    raw_result=raw_result,
                    authority_score=0.8,
                    relevance_score=0.5,
                    utility_score=0.7,
                    supports=["retrieved_evidence"],
                )
            )
        return evidence

    def _is_evidence_producing_tool(self, tool_name: str) -> bool:
        return (
            tool_name in self.EVIDENCE_PRODUCING_TOOLS
            or tool_name.startswith("openapi_") and "_get_" in tool_name
        )

    def _extract_cite_uids(self, value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, Mapping):
            item = value.get("cite_uid")
            if isinstance(item, str) and item:
                found.append(item)
            for child in value.values():
                found.extend(self._extract_cite_uids(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(self._extract_cite_uids(child))
        elif isinstance(value, str):
            found.extend(match.group(1) for match in self.CITE_UID_PATTERN.finditer(value))
        return found

    async def _chat_completion(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not self.settings.model_api_base:
            raise RuntimeError("Lunit L2 model endpoint is not configured.")
        headers: dict[str, str] = {}
        if self.settings.model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.model_api_key}"
        payload = {
            "model": self.settings.model_name,
            "temperature": 0,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        url = self.settings.model_api_base.rstrip("/") + "/chat/completions"
        if self._http_client is not None:
            response = await self._http_client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def _generation_system_prompt(self) -> str:
        base = (
            "You are Lunit L2 generating the final medical answer. Use only the "
            "retrieve_relevant_content tool when external evidence is needed, and "
            "call it at most once for a user request. After the tool returns, do not "
            "call it again; write the final user-facing answer from the retrieved "
            "evidence. Do not call MCP tools directly in this phase. Cite retrieved "
            "evidence markers."
        )
        if self.answer_prompt.strip():
            return f"{base}\n\n{self.answer_prompt.strip()}"
        return base

    def _retrieval_system_prompt(self) -> str:
        return (
            "You are in retrieval phase only. Your only job is to gather citation "
            "evidence, then end this phase by calling finalize_retrieval exactly "
            "once. Do not write the final user answer. Do not stop after list/search "
            "tools when a content/detail tool is available.\n\n"
            "Common rule: after any relevant evidence content, detail, article, "
            "abstract, or cite_uid is found, immediately call finalize_retrieval. "
            "If cite_uid values exist, include each cite_uid with a relevance_score. "
            "If relevant content exists but no cite_uid exists, call "
            "finalize_retrieval with status partial and explain the missing cite_uid. "
            "If no useful evidence exists, call finalize_retrieval with status "
            "no_evidence.\n\n"
            "Guideline or HIRA document flow: first use index_get_relevant_nodes or "
            "index_keyword_search to narrow the document and page range. Then call "
            "index_get_page_content for the relevant pages before finalizing. Prefer "
            "the cite_uid returned by index_get_page_content.\n\n"
            "Korean HIRA or coverage flow: use HIRA/index tools for coverage criteria "
            "and public review material. For document-like results, still narrow to "
            "nodes/pages and call index_get_page_content before finalizing.\n\n"
            "Korean law flow: call openapi_law_search, then "
            "openapi_law_list_articles, then openapi_law_get_article. Finalize with "
            "the citable article/detail result.\n\n"
            "Medication flow: prefer adr_retrieve_drug_info or MFDS detail tools. "
            "Finalize from label, indication, contraindication, warning, interaction, "
            "or dosage detail records rather than search-only candidates.\n\n"
            "PubMed flow: use rag_vector_query on PubMed-style queries and preserve "
            "cite_uid, URL, title, and abstract-like text from returned items."
        )

    def _build_generation_context(
        self,
        message: str,
        state: ClinicalState,
        analysis: QueryAnalysis,
        requirements: ResponseRequirements,
        seed_evidence: list[RetrievalEvidence],
    ) -> str:
        evidence_text = "\n".join(
            f"[{item.id}] {item.title}: {item.content}" for item in seed_evidence
        )
        return (
            f"User message: {message}\n"
            f"Clinical state: {state.model_dump_json()}\n"
            f"Query analysis: {analysis.model_dump_json()}\n"
            f"Response requirements: {requirements.model_dump_json()}\n"
            f"Seed evidence, if any:\n{evidence_text}"
        )

    def _normalize_selection(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": arguments.get("status", "partial"),
            "items": list(arguments.get("items") or []),
            "note": str(arguments.get("note") or ""),
        }

    def _evidence_from_selection(
        self, selection: dict[str, Any], tool_results: list[dict[str, Any]]
    ) -> list[RetrievalEvidence]:
        items = selection.get("items") or []
        selected: list[RetrievalEvidence] = []
        for item in items:
            cite_uid = str(item.get("cite_uid") or "")
            match = self._find_result_with_cite_uid(tool_results, cite_uid)
            raw_result = match.get("result") if match else {"cite_uid": cite_uid}
            selected.append(
                RetrievalEvidence(
                    id=cite_uid or f"lunit-{uuid4()}",
                    source="lunit_mcp",
                    title=self._first_string(raw_result, ["title", "name", "source"]) or cite_uid,
                    content=self._tool_result_text(raw_result),
                    cite_uid=cite_uid or None,
                    source_url=self._first_string(raw_result, ["url", "source_url", "link"]),
                    corpus_tag=self._first_string(raw_result, ["corpus_tag", "source_type"]),
                    page_range=self._page_range(raw_result),
                    raw_tool_name=str(match.get("tool_name")) if match else None,
                    raw_result=raw_result,
                    authority_score=1.0,
                    relevance_score=float(item.get("relevance_score") or 0.0),
                    utility_score=1.0,
                    supports=["retrieved_evidence"],
                )
            )
        return selected

    def _find_result_with_cite_uid(
        self, tool_results: list[dict[str, Any]], cite_uid: str
    ) -> dict[str, Any] | None:
        for tool_result in tool_results:
            if self._contains_value(tool_result.get("result"), cite_uid):
                return tool_result
        return None

    def _contains_value(self, value: Any, needle: str) -> bool:
        if not needle:
            return False
        if isinstance(value, str):
            return needle in value
        if isinstance(value, Mapping):
            return any(self._contains_value(item, needle) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_value(item, needle) for item in value)
        return False

    def _first_string(self, value: Any, keys: list[str]) -> str | None:
        if isinstance(value, Mapping):
            for key in keys:
                item = value.get(key)
                if isinstance(item, str) and item:
                    return item
            for item in value.values():
                found = self._first_string(item, keys)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._first_string(item, keys)
                if found:
                    return found
        return None

    def _page_range(self, value: Any) -> str | None:
        if isinstance(value, Mapping):
            start = value.get("start_page")
            end = value.get("end_page")
            if start is not None and end is not None:
                return f"{start}-{end}"
            page = value.get("page")
            if page is not None:
                return str(page)
            for item in value.values():
                found = self._page_range(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._page_range(item)
                if found:
                    return found
        return None

    def _format_evidence_for_generation(
        self, status: str, evidence: list[RetrievalEvidence]
    ) -> str:
        if not evidence:
            return f"status: {status}\nNo citable evidence found."
        lines = [f"status: {status}"]
        for index, item in enumerate(evidence, start=1):
            lines.append(
                "\n".join(
                    [
                        f"[{index}]",
                        f"cite_uid: {item.cite_uid or item.id}",
                        f"source_type: {item.corpus_tag or item.source}",
                        f"url: {item.source_url or ''}",
                        f"title: {item.title or ''}",
                        f"content: {self._truncate(item.content, self.settings.l2_tool_result_char_limit)}",
                    ]
                )
            )
        return "\n\n".join(lines)

    def _retrieval_tool_response_text(self, result: Any) -> str:
        content = self._tool_result_text(
            result, limit=self.settings.l2_tool_result_char_limit
        )
        if not content or content in {"{}", "[]", "null"}:
            return (
                f"{content}\n\n"
                "Retrieval controller reminder: if no useful evidence was found, "
                "call finalize_retrieval with status no_evidence now."
            )
        if self._extract_cite_uids(result):
            return (
                f"{content}\n\n"
                "Retrieval controller reminder: cite_uid values are available in "
                "this result. Call finalize_retrieval exactly once now and include "
                "the relevant cite_uid values."
            )
        return (
            f"{content}\n\n"
            "Retrieval controller reminder: if this content is relevant, call "
            "finalize_retrieval exactly once now. If it lacks cite_uid, use status "
            "partial and explain that cite_uid is missing."
        )

    def _tool_result_text(self, result: Any, limit: int | None = None) -> str:
        if isinstance(result, Mapping):
            content = result.get("content")
            if isinstance(content, list):
                texts = [
                    str(item.get("text"))
                    for item in content
                    if isinstance(item, Mapping) and item.get("type") == "text"
                ]
                if texts:
                    return self._truncate("\n".join(texts), limit)
            structured = result.get("structuredContent") or result.get("structured_content")
            if structured is not None:
                return self._truncate(
                    json.dumps(structured, ensure_ascii=False, sort_keys=True), limit
                )
        return self._truncate(json.dumps(result, ensure_ascii=False, sort_keys=True), limit)

    def _truncate(self, text: str, limit: int | None) -> str:
        if limit is None or limit <= 0 or len(text) <= limit:
            return text
        return text[:limit] + "\n[truncated]"

    def _parse_arguments(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if raw is None or raw == "":
            return {}
        parsed = json.loads(str(raw))
        if not isinstance(parsed, dict):
            return {}
        return parsed
