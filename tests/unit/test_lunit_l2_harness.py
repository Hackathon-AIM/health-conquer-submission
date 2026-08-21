import asyncio
import json

import httpx

from medibot.config.settings import Settings
from medibot.core.schemas import ClinicalState, QueryAnalysis, ResponseRequirements
from medibot.reasoning.lunit_harness import LunitL2Harness
from medibot.sources.mcp_client import LunitMCPClient


def test_generation_phase_exposes_only_retrieve_tool() -> None:
    harness = LunitL2Harness(Settings())
    tools = harness.generation_tools()

    assert [tool["function"]["name"] for tool in tools] == ["retrieve_relevant_content"]


def test_generation_prompt_limits_retrieve_tool_reuse() -> None:
    harness = LunitL2Harness(Settings())
    prompt = harness._generation_system_prompt()

    assert "call it at most once" in prompt
    assert "After the tool returns, do not call it again" in prompt


def test_mcp_sse_response_parser_returns_final_jsonrpc_result() -> None:
    client = LunitMCPClient(Settings())
    parsed = client.parse_sse_response(
        'event: message\n'
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}\n\n'
    )

    assert parsed["result"]["content"][0]["text"] == "ok"


def test_retrieval_phase_exposes_mcp_tools_and_finalize() -> None:
    settings = Settings(
        model_api_base="https://lunit.test/v1",
        model_name="Lunit/L2-preview",
        model_api_key="lunit_test",
        mcp_url="https://mcp.test/mcp",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["method"] == "tools/list"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "tools": [
                        {
                            "name": "index_get_page_content",
                            "description": "Get citable page content.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                },
            },
        )

    async def run() -> list[dict]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            harness = LunitL2Harness(
                settings,
                mcp_client=LunitMCPClient(settings, http_client=client),
                http_client=client,
            )
            return await harness.retrieval_tools()

    tools = asyncio.run(run())

    assert [tool["function"]["name"] for tool in tools] == [
        "index_get_page_content",
        "finalize_retrieval",
    ]


def test_mcp_tool_selection_uses_deterministic_priority_order() -> None:
    harness = LunitL2Harness(Settings())
    unordered_tools = [
        {"name": "index_get_page_content"},
        {"name": "adr_retrieve_drug_info"},
        {"name": "index_keyword_search"},
        {"name": "index_get_document_structure"},
        {"name": "index_get_relevant_nodes"},
        {"name": "index_list_documents"},
    ]

    selected = harness._select_mcp_tools(
        unordered_tools,
        "chronic kidney disease blood pressure target guideline recommendation",
    )

    assert [tool["name"] for tool in selected] == [
        "index_list_documents",
        "index_get_relevant_nodes",
        "index_keyword_search",
        "index_get_page_content",
    ]


def test_retrieval_prompt_contains_source_specific_trajectories() -> None:
    harness = LunitL2Harness(Settings())
    prompt = harness._retrieval_system_prompt()

    assert "finalize_retrieval exactly once" in prompt
    assert "index_get_relevant_nodes or index_keyword_search" in prompt
    assert "index_get_page_content" in prompt
    assert "openapi_law_search" in prompt
    assert "openapi_law_list_articles" in prompt
    assert "openapi_law_get_article" in prompt
    assert "Medication flow" in prompt
    assert "PubMed flow" in prompt


def test_source_specific_tool_selection() -> None:
    harness = LunitL2Harness(Settings())
    tools = [
        {"name": "index_get_relevant_nodes"},
        {"name": "index_keyword_search"},
        {"name": "index_get_page_content"},
        {"name": "hira_updates_search"},
        {"name": "rag_vector_query"},
        {"name": "openapi_law_search"},
        {"name": "openapi_law_list_articles"},
        {"name": "openapi_law_get_article"},
        {"name": "adr_retrieve_drug_info"},
        {"name": "openapi_mfds_get_drug_indication"},
        {"name": "openapi_mfds_check_drug_permission"},
        {"name": "openapi_mfds_find_drugs_by_ingredient"},
    ]

    hira = harness._select_mcp_tools(tools, "심평원 HIRA 급여 coverage")
    law = harness._select_mcp_tools(tools, "한국 의료법 조문 statute")
    medication = harness._select_mcp_tools(tools, "ibuprofen drug interaction dose")
    pubmed = harness._select_mcp_tools(tools, "latest PubMed study evidence")

    assert [tool["name"] for tool in hira][:3] == [
        "index_get_relevant_nodes",
        "index_keyword_search",
        "index_get_page_content",
    ]
    assert [tool["name"] for tool in law] == [
        "openapi_law_search",
        "openapi_law_list_articles",
        "openapi_law_get_article",
    ]
    assert [tool["name"] for tool in medication] == [
        "adr_retrieve_drug_info",
        "openapi_mfds_get_drug_indication",
        "openapi_mfds_check_drug_permission",
        "openapi_mfds_find_drugs_by_ingredient",
    ]
    assert [tool["name"] for tool in pubmed] == ["rag_vector_query"]


def test_budget_fallback_uses_only_evidence_producing_tool_results() -> None:
    harness = LunitL2Harness(Settings(evidence_limit=3))
    tool_results = [
        {
            "tool_name": "index_list_documents",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Only a document list, not final evidence.",
                    }
                ]
            },
        },
        {
            "tool_name": "index_get_page_content",
            "result": {
                "content": [
                    {
                        "type": "text",
                            "text": "Citable guideline content.",
                        }
                    ]
            },
        },
    ]

    evidence = harness._evidence_from_tool_results(tool_results)

    assert len(evidence) == 1
    assert evidence[0].raw_tool_name == "index_get_page_content"
    assert "Citable guideline content" in evidence[0].content


def test_nested_cite_uid_extraction() -> None:
    harness = LunitL2Harness(Settings())

    cite_uids = harness._extract_cite_uids(
        {
            "items": [
                {"metadata": {"cite_uid": "cite-a"}},
                {"children": [{"cite_uid": "cite-b"}]},
            ],
            "content": [{"type": "text", "text": "page text cite_uid=cite-c"}],
        }
    )

    assert cite_uids == ["cite-a", "cite-b", "cite-c"]


def test_fallback_evidence_preserves_cite_uid_from_content_text() -> None:
    harness = LunitL2Harness(Settings(evidence_limit=3))
    evidence = harness._evidence_from_tool_results(
        [
            {
                "tool_name": "index_get_page_content",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Citable page content cite_uid=cite-text-1",
                        }
                    ]
                },
            }
        ]
    )

    assert evidence[0].id == "cite-text-1"
    assert evidence[0].cite_uid == "cite-text-1"
    assert evidence[0].raw_tool_name == "index_get_page_content"


def test_article_and_detail_tools_are_evidence_producing() -> None:
    harness = LunitL2Harness(Settings())

    assert harness._is_evidence_producing_tool("openapi_law_get_article") is True
    assert harness._is_evidence_producing_tool("openapi_mfds_get_drug_indication") is True


def test_retrieval_tool_response_reminds_l2_to_finalize() -> None:
    harness = LunitL2Harness(Settings())

    content = harness._retrieval_tool_response_text(
        {
            "content": [
                {
                    "type": "text",
                    "text": "Citable page content cite_uid=cite-reminder-1",
                }
            ]
        }
    )

    assert "Call finalize_retrieval exactly once now" in content
    assert "cite-reminder-1" in content


def test_lunit_harness_runs_generation_retrieval_and_preserves_cite_uid() -> None:
    settings = Settings(
        model_api_base="https://lunit.test/v1",
        model_name="Lunit/L2-preview",
        model_api_key="lunit_test",
        mcp_url="https://mcp.test/mcp",
        final_model_provider="lunit_l2",
        require_l2_final=True,
        allow_fallback=False,
    )
    chat_calls: list[dict] = []
    mcp_call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mcp_call_count
        payload = json.loads(request.content)
        if str(request.url).startswith(settings.mcp_url):
            if payload["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "index_get_page_content",
                                    "description": "Get citable page content.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "query": {"type": "string"},
                                        },
                                    },
                                }
                            ]
                        },
                    },
                )
            if payload["method"] == "tools/call":
                mcp_call_count += 1
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "CKD guideline says target SBP under 120 when tolerated. cite_uid=cite-ckd-1",
                                }
                            ],
                            "structuredContent": {
                                "cite_uid": "cite-ckd-1",
                                "title": "CKD Blood Pressure Guideline",
                                "url": "https://example.test/guideline",
                                "corpus_tag": "guideline",
                                "start_page": 48,
                                "end_page": 52,
                            },
                        },
                    },
                )
        chat_calls.append(payload)
        messages = payload["messages"]
        tools = [tool["function"]["name"] for tool in payload["tools"]]
        if tools == ["retrieve_relevant_content"] and messages[-1]["role"] == "user":
            return _chat_response(
                payload["model"],
                {
                    "tool_calls": [
                        _tool_call(
                            "gen-call-1",
                            "retrieve_relevant_content",
                            {"query": "blood pressure target CKD guideline"},
                        )
                    ],
                    "content": None,
                },
            )
        if "finalize_retrieval" in tools and messages[-1]["role"] == "user":
            return _chat_response(
                payload["model"],
                {
                    "tool_calls": [
                        _tool_call(
                            "ret-call-1",
                            "index_get_page_content",
                            {"query": "blood pressure target CKD guideline"},
                        )
                    ],
                    "content": None,
                },
            )
        if "finalize_retrieval" in tools and messages[-1]["role"] == "tool":
            return _chat_response(
                payload["model"],
                {
                    "tool_calls": [
                        _tool_call(
                            "ret-call-2",
                            "finalize_retrieval",
                            {
                                "status": "sufficient",
                                "items": [
                                    {
                                        "cite_uid": "cite-ckd-1",
                                        "relevance_score": 0.97,
                                    }
                                ],
                                "note": "Found guideline page.",
                            },
                        )
                    ],
                    "content": None,
                },
            )
        return _chat_response(
            payload["model"],
            {
                "content": "가이드라인 근거에 따르면 내약 가능한 경우 목표는 수축기 혈압 120 mmHg 미만입니다 [cite-ckd-1]."
            },
        )

    async def run() -> tuple[str, LunitL2Harness]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            mcp_client = LunitMCPClient(settings, http_client=client)
            harness = LunitL2Harness(settings, mcp_client=mcp_client, http_client=client)
            answer = await harness.generate(
                "만성 신장질환 혈압 목표는?",
                ClinicalState(session_id="test"),
                QueryAnalysis(intent="clinical_guidance", retrieval_need="required"),
                ResponseRequirements(),
                [],
            )
            return answer, harness

    answer, harness = asyncio.run(run())

    assert "120 mmHg" in answer
    assert mcp_call_count == 1
    assert harness.last_trace["l2_retrieval_phase_status"] == "sufficient"
    assert harness.last_trace["l2_mcp_tool_call_count"] == 1
    assert harness.last_trace["l2_selected_cite_uids"] == ["cite-ckd-1"]
    assert harness.last_evidence[0].cite_uid == "cite-ckd-1"
    assert harness.last_evidence[0].raw_tool_name == "index_get_page_content"
    assert chat_calls[0]["tools"][0]["function"]["name"] == "retrieve_relevant_content"


def test_retrieval_fallback_preserves_cite_uid_when_finalize_is_missing() -> None:
    settings = Settings(
        model_api_base="https://lunit.test/v1",
        model_name="Lunit/L2-preview",
        model_api_key="lunit_test",
        mcp_url="https://mcp.test/mcp",
        l2_retrieval_tool_budget=2,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if str(request.url).startswith(settings.mcp_url):
            if payload["method"] == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "index_get_page_content",
                                    "description": "Get citable page content.",
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Guideline page content cite_uid=cite-missing-finalize",
                            }
                        ]
                    },
                },
            )

        messages = payload["messages"]
        if messages[-1]["role"] == "user":
            return _chat_response(
                payload["model"],
                {
                    "tool_calls": [
                        _tool_call("ret-1", "index_get_page_content", {"query": "bp"})
                    ],
                    "content": None,
                },
            )
        return _chat_response(payload["model"], {"content": ""})

    async def run() -> tuple[dict, list]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            harness = LunitL2Harness(
                settings,
                mcp_client=LunitMCPClient(settings, http_client=client),
                http_client=client,
            )
            harness.last_trace = {"l2_mcp_tool_call_count": 0}
            return await harness._run_retrieval_phase("blood pressure guideline")

    selection, evidence = asyncio.run(run())

    assert selection["status"] == "partial"
    assert selection["items"][0]["cite_uid"] == "cite-missing-finalize"
    assert evidence[0].cite_uid == "cite-missing-finalize"
    assert evidence[0].raw_tool_name == "index_get_page_content"


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _chat_response(model: str, message: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        **message,
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )
