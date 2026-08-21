import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from medibot.config.settings import Settings
from medibot.core.schemas import ChatMessage, MedibotRequest, RetrievalEvidence
from medibot.evaluation.coeval_client import MediBotCoEvalClient
from medibot.orchestrator.workflow import MedibotWorkflow
from medibot.reasoning.generator import MedicalGenerator
from medibot.reasoning.lunit_harness import LunitL2Harness
from medibot.sources.mcp_client import LunitMCPClient


def test_workflow_emergency_skips_retrieval_and_writes_trace(tmp_path: Path) -> None:
    async def run() -> None:
        workflow = MedibotWorkflow(settings=Settings(trace_path=tmp_path / "trace.jsonl"))
        response = await workflow.handle(
            MedibotRequest(
                messages=[ChatMessage(role="user", content="심한 가슴 통증이 있어요")]
            )
        )
        assert response.triage_class == "EMERGENT"
        assert response.retrieval_used is False
        assert "119" in response.answer
        trace_lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(trace_lines) == 1
        assert json.loads(trace_lines[0])["triage_class"] == "EMERGENT"

    asyncio.run(run())


def test_cli_smoke_runner_writes_trace(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["MEDIBOT_TRACE_PATH"] = str(tmp_path / "cli-trace.jsonl")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "medibot.evaluation.smoke",
            "--message",
            "고혈압약 먹는데 이부프로펜 먹어도 돼?",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["answer"]
    assert output["retrieval_used"] is True
    assert (tmp_path / "cli-trace.jsonl").exists()


def test_coeval_client_messages_to_answer(tmp_path: Path) -> None:
    async def run() -> None:
        workflow = MedibotWorkflow(settings=Settings(trace_path=tmp_path / "coeval.jsonl"))
        client = MediBotCoEvalClient(workflow=workflow)
        answer = await client.generate(
            [{"role": "user", "content": "고혈압약 먹는데 이부프로펜 먹어도 돼?"}]
        )
        assert "이부프로펜" in answer

    asyncio.run(run())


def test_postpartum_plan_is_not_replaced_by_generic_fallback(tmp_path: Path) -> None:
    async def run() -> None:
        workflow = MedibotWorkflow(
            settings=Settings(
                model_api_base=None,
                model_name="medibot-fallback",
                trace_path=tmp_path / "postpartum.jsonl",
            )
        )
        response = await workflow.handle(
            MedibotRequest(
                messages=[
                    ChatMessage(
                        role="user",
                        content="Can you make a 3-month plan for postpartum depression?",
                    )
                ]
            )
        )
        assert response.safety_status == "pass"
        assert "3-month" in response.answer
        assert "therapy" in response.answer.lower()
        assert "self-harm" in response.answer.lower()

    asyncio.run(run())


def test_lab_uncertainty_answer_requests_core_context(tmp_path: Path) -> None:
    async def run() -> None:
        workflow = MedibotWorkflow(
            settings=Settings(
                model_api_base=None,
                model_name="medibot-fallback",
                trace_path=tmp_path / "lab.jsonl",
            )
        )
        response = await workflow.handle(
            MedibotRequest(
                messages=[
                    ChatMessage(
                        role="user",
                        content="My lab result is abnormal but I am not sure what it means.",
                    )
                ]
            )
        )
        lowered = response.answer.lower()
        assert response.safety_status == "pass"
        assert "test name" in lowered
        assert "exact value" in lowered
        assert "reference range" in lowered

    asyncio.run(run())


def test_supplement_interaction_answer_requests_product_names(tmp_path: Path) -> None:
    async def run() -> None:
        workflow = MedibotWorkflow(
            settings=Settings(
                model_api_base=None,
                model_name="medibot-fallback",
                trace_path=tmp_path / "supplement.jsonl",
            )
        )
        response = await workflow.handle(
            MedibotRequest(
                messages=[
                    ChatMessage(
                        role="user",
                        content="Can my herbal supplement interfere with my blood pressure medication?",
                    )
                ]
            )
        )
        lowered = response.answer.lower()
        assert response.safety_status == "pass"
        assert "supplement name" in lowered
        assert "blood pressure medicine name" in lowered
        assert "dose" in lowered

    asyncio.run(run())


def test_lunit_l2_workflow_uses_mcp_harness_and_writes_trace(tmp_path: Path) -> None:
    async def run() -> None:
        settings = Settings(
            model_api_base="https://lunit.test/v1",
            model_name="Lunit/L2-preview",
            model_api_key="lunit_test",
            mcp_url="https://mcp.test/mcp",
            final_model_provider="lunit_l2",
            require_l2_final=True,
            allow_fallback=False,
            trace_path=tmp_path / "l2.jsonl",
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
                                    "text": "Guideline content with cite_uid=cite-guideline-1",
                                }
                            ],
                            "structuredContent": {
                                "cite_uid": "cite-guideline-1",
                                "title": "Guideline",
                                "url": "https://example.test/guideline",
                                "corpus_tag": "guideline",
                            },
                        },
                    },
                )

            messages = payload["messages"]
            tool_names = [tool["function"]["name"] for tool in payload.get("tools", [])]
            if tool_names == ["retrieve_relevant_content"] and messages[-1]["role"] == "user":
                return _workflow_chat_response(
                    settings.model_name,
                    {
                        "content": None,
                        "tool_calls": [
                            _workflow_tool_call(
                                "gen-1",
                                "retrieve_relevant_content",
                                {"query": "guideline blood pressure target"},
                            )
                        ],
                    },
                )
            if "finalize_retrieval" in tool_names and messages[-1]["role"] == "user":
                return _workflow_chat_response(
                    settings.model_name,
                    {
                        "content": None,
                        "tool_calls": [
                            _workflow_tool_call(
                                "ret-1",
                                "index_get_page_content",
                                {"query": "guideline blood pressure target"},
                            )
                        ],
                    },
                )
            if "finalize_retrieval" in tool_names and messages[-1]["role"] == "tool":
                return _workflow_chat_response(
                    settings.model_name,
                    {
                        "content": None,
                        "tool_calls": [
                            _workflow_tool_call(
                                "ret-2",
                                "finalize_retrieval",
                                {
                                    "status": "sufficient",
                                    "items": [
                                        {
                                            "cite_uid": "cite-guideline-1",
                                            "relevance_score": 0.9,
                                        }
                                    ],
                                    "note": "ok",
                                },
                            )
                        ],
                    },
                )
            return _workflow_chat_response(
                settings.model_name,
                {"content": "근거에 따르면 혈압 목표를 개별화해 조절합니다 [cite-guideline-1]."},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            harness = LunitL2Harness(
                settings,
                mcp_client=LunitMCPClient(settings, http_client=client),
                http_client=client,
            )
            workflow = MedibotWorkflow(settings=settings)
            workflow.generator = MedicalGenerator(settings, lunit_harness=harness)
            response = await workflow.handle(
                MedibotRequest(
                    messages=[ChatMessage(role="user", content="혈압 목표 가이드라인은?")]
                )
            )

        trace = json.loads((tmp_path / "l2.jsonl").read_text(encoding="utf-8"))
        assert response.retrieval_used is True
        assert response.evidence_ids == ["cite-guideline-1"]
        assert trace["generation_mode"] == "remote:lunit_l2:harness"
        assert trace["l2_retrieval_phase_status"] == "sufficient"
        assert trace["l2_mcp_tool_call_count"] == 1
        assert trace["l2_selected_cite_uids"] == ["cite-guideline-1"]
        assert trace["l2_finalize_note"] == "ok"

    asyncio.run(run())


def test_workflow_trace_omits_raw_evidence_payload(tmp_path: Path) -> None:
    async def run() -> None:
        settings = Settings(trace_path=tmp_path / "safe-trace.jsonl")
        workflow = MedibotWorkflow(settings=settings)
        workflow.generator.last_evidence = [
            RetrievalEvidence(
                id="e1",
                source="lunit_mcp",
                title="Large evidence",
                content="x" * 1200,
                cite_uid="cite-safe",
                raw_result={"secret": "raw payload should not be serialized"},
                raw_tool_name="index_get_page_content",
            )
        ]
        trace = workflow._build_trace(
            trace_id="trace-safe",
            state_session_id="session",
            user_message="question",
            assistant_response="answer",
            jurisdiction=None,
            triage_class="NON_EMERGENT",
            analysis=workflow.analyzer.analyze("콜레스테롤이 뭐야?", "NON_EMERGENT"),
            source_queries={},
            selected_sources=[],
            raw_evidence=workflow.generator.last_evidence,
            final_evidence=workflow.generator.last_evidence,
            retrieval_diagnostics=[],
            coverage_result={},
            claims=[],
            groundedness_result={},
            safety_result={},
            fallback_reason=None,
            started=0.0,
        )
        workflow.trace_writer.write(trace)

    asyncio.run(run())
    trace = json.loads((tmp_path / "safe-trace.jsonl").read_text(encoding="utf-8"))
    evidence = trace["retrieval_results"][0]
    assert "raw_result" not in evidence
    assert "raw payload should not be serialized" not in json.dumps(trace)
    assert evidence["content_preview"].endswith("[truncated]")
    assert evidence["cite_uid"] == "cite-safe"


def test_lunit_l2_failure_writes_trace_and_reraises(tmp_path: Path) -> None:
    async def run() -> None:
        settings = Settings(
            model_api_base="https://lunit.test/v1",
            model_name="Lunit/L2-preview",
            model_api_key="lunit_test",
            mcp_url="https://mcp.test/mcp",
            final_model_provider="lunit_l2",
            require_l2_final=True,
            allow_fallback=False,
            trace_path=tmp_path / "failure.jsonl",
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            harness = LunitL2Harness(
                settings,
                mcp_client=LunitMCPClient(settings, http_client=client),
                http_client=client,
            )
            workflow = MedibotWorkflow(settings=settings)
            workflow.generator = MedicalGenerator(settings, lunit_harness=harness)
            try:
                await workflow.handle(
                    MedibotRequest(
                        messages=[
                            ChatMessage(
                                role="user",
                                content="혈압 목표 가이드라인은?",
                            )
                        ]
                    )
                )
            except httpx.HTTPStatusError:
                return
            raise AssertionError("Expected L2 failure to be re-raised")

    asyncio.run(run())
    trace = json.loads((tmp_path / "failure.jsonl").read_text(encoding="utf-8"))
    assert trace["generation_error"] == "lunit_l2_harness_failed"
    assert trace["final_model_provider"] == "lunit_l2"
    assert trace["l2_required"] is True
    assert trace["safety_result"]["blocking_issues"] == ["pipeline_exception"]


def _workflow_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _workflow_chat_response(model: str, message: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", **message},
                    "finish_reason": "stop",
                }
            ],
        },
    )
