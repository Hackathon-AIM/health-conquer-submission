from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from submission.config import Settings
from submission.context import TokenCounter
from submission.mcp import LunitMCPClient, _parse_sse
from submission.toolspec import build_tools
from submission.model import ChatResponse, LunitModelClient
from submission.orchestrator import NativeDriver
from submission.routing import clarification_question, detect_persona


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class ScriptedModel:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)
        self.calls = 0
        self.requests: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def chat(self, *args, **kwargs) -> ChatResponse:
        self.calls += 1
        self.requests.append((args, kwargs))
        message = self.messages.pop(0)
        return ChatResponse(message=message, raw={"choices": [{"message": message}]})


class FakeMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "name": "index_get_relevant_nodes",
            "description": "find nodes",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": json.dumps({
            "cite_uid": "cite-real",
            "title": "가이드라인",
            "content": "내약 가능하면 목표를 개별화한다.",
        }, ensure_ascii=False)}]}


class SubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            fm_api_key="test",
            max_retrieval_steps=4,
            max_tool_calls=2,
            max_retrieval_calls=1,
        )

    def test_direct_generation_needs_no_retrieval(self) -> None:
        model = ScriptedModel([{"role": "assistant", "content": "직접 답변"}])
        driver = NativeDriver(self.settings, model=model, mcp=FakeMCP())
        self.assertEqual(driver.answer([{"role": "user", "content": "감기 때 물을 마셔도 되나요?"}]), "직접 답변")
        self.assertEqual(model.calls, 1)

    def test_two_stage_flow_rejects_unobserved_citation(self) -> None:
        model = ScriptedModel([
            {"role": "assistant", "content": None, "tool_calls": [_call("g1", "retrieve_relevant_content", {"query": "CKD 혈압 목표"})]},
            {"role": "assistant", "content": None, "tool_calls": [_call("r1", "index_get_relevant_nodes", {"query": "CKD BP target"})]},
            {"role": "assistant", "content": None, "tool_calls": [_call("r2", "finalize_retrieval", {
                "status": "sufficient",
                "items": [
                    {"cite_uid": "cite-fake", "relevance_score": 1.0},
                    {"cite_uid": "cite-real", "relevance_score": 0.9},
                ],
            })]},
            {"role": "assistant", "content": "근거를 반영한 답변 [1]"},
        ])
        mcp = FakeMCP()
        driver = NativeDriver(self.settings, model=model, mcp=mcp)
        answer = driver.answer([{"role": "user", "content": "신장질환이 있으면 혈압 목표가 어떻게 되나요?"}])
        self.assertEqual(answer, "근거를 반영한 답변 [1]")
        self.assertEqual(len(mcp.calls), 1)
        self.assertEqual(model.calls, 4)

    def test_evidence_request_forces_retrieval_before_generation(self) -> None:
        model = ScriptedModel([
            {"role": "assistant", "content": None, "tool_calls": [_call("r1", "index_get_relevant_nodes", {"query": "AC joint type IIIB"})]},
            {"role": "assistant", "content": None, "tool_calls": [_call("r2", "finalize_retrieval", {
                "status": "sufficient",
                "items": [{"cite_uid": "cite-real", "relevance_score": 0.9}],
            })]},
            {"role": "assistant", "content": "확인된 연구 근거를 반영한 답변 [1]"},
        ])
        mcp = FakeMCP()
        driver = NativeDriver(self.settings, model=model, mcp=mcp)
        answer = driver.answer([{
            "role": "user",
            "content": "3B형도 수술 없이 80% 이상 낫는다는 연구 결과가 진짜 있나요?",
        }])
        self.assertEqual(answer, "확인된 연구 근거를 반영한 답변 [1]")
        self.assertEqual(len(mcp.calls), 1)
        self.assertEqual(model.calls, 3)

    def test_drug_causality_question_requires_retrieval(self) -> None:
        messages = [{"role": "user", "content": "세툭시맙 맞으면서 변비가 심해졌어요. 약 때문인가요?"}]
        self.assertTrue(NativeDriver._requires_retrieval(messages))

    def test_missing_drug_name_asks_one_question_without_retrieval(self) -> None:
        messages = [{"role": "user", "content": "약 먹고 있는데 두통약 같이 먹어도 되나요?"}]
        self.assertIn("정확한", clarification_question(messages))
        model = ScriptedModel([{
            "role": "assistant",
            "content": "약에 따라 달라집니다. 현재 약의 정확한 제품명이나 성분명이 무엇인가요?",
        }])
        mcp = FakeMCP()
        answer = NativeDriver(self.settings, model=model, mcp=mcp).answer(messages)
        self.assertEqual(answer.count("?"), 1)
        self.assertEqual(mcp.calls, [])
        self.assertEqual(model.calls, 0)
        self.assertNotIn("\n1.", answer)

    def test_short_followup_uses_previous_drug_context(self) -> None:
        messages = [
            {"role": "user", "content": "약 먹고 있는데 두통약 같이 먹어도 되나요?"},
            {"role": "assistant", "content": "정확한 약 이름을 알려주세요."},
            {"role": "user", "content": "아, 아스피린이요."},
        ]
        self.assertEqual(clarification_question(messages), "")
        self.assertTrue(NativeDriver._requires_retrieval(messages))

    def test_professional_persona_changes_system_prompt(self) -> None:
        messages = [{"role": "user", "content": "45세 남성 eGFR 38, ACEi 유지 vs 중단?"}]
        self.assertEqual(detect_persona(messages), "professional")
        model = ScriptedModel([{"role": "assistant", "content": "ACEi 유지 여부 답변"}])
        NativeDriver(self.settings, model=model, mcp=FakeMCP()).answer(messages)
        system = model.requests[0][0][0][0]["content"]
        self.assertIn("의료전문가", system)

    def test_assistant_medical_terms_do_not_change_user_persona(self) -> None:
        messages = [
            {"role": "user", "content": "제가 먹는 약이 걱정돼요."},
            {"role": "assistant", "content": "ACEi와 eGFR을 함께 검토합니다."},
            {"role": "user", "content": "그럼 저는 어떻게 해야 하나요?"},
        ]
        self.assertEqual(detect_persona(messages), "lay")

    def test_trace_logs_route_and_completion_without_question_text(self) -> None:
        question = "감기 때 물을 마셔도 되나요?"
        model = ScriptedModel([{"role": "assistant", "content": "직접 답변"}])
        with self.assertLogs("driver.orchestrator", level="INFO") as captured:
            NativeDriver(self.settings, model=model, mcp=FakeMCP()).answer([
                {"role": "user", "content": question},
            ])
        joined = "\n".join(captured.output)
        self.assertIn('"event":"route"', joined)
        self.assertIn('"event":"request_complete"', joined)
        self.assertNotIn(question, joined)

    def test_drug_query_prunes_unrelated_tool_schemas(self) -> None:
        definitions = [
            {"name": "adr_retrieve_drug_info"},
            {"name": "openapi_mfds_get_drug_indication"},
            {"name": "rag_vector_query"},
            {"name": "openapi_law_search"},
            {"name": "kcd_get_name"},
        ]
        tools, diagnostics = build_tools(
            definitions, "세툭시맙 약 부작용", TokenCounter(), token_budget=400, max_tools=4,
        )
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("adr_retrieve_drug_info", names)
        # 법령·질병코드 도구는 이 질문과 무관하다. 좁은 창에서 이게 실리면 근거가 밀린다.
        self.assertNotIn("openapi_law_search", names)
        self.assertNotIn("kcd_get_name", names)
        self.assertFalse(diagnostics.get("starved"))

    def test_emergency_short_circuits_model(self) -> None:
        model = ScriptedModel([])
        driver = NativeDriver(self.settings, model=model, mcp=FakeMCP())
        answer = driver.answer([{"role": "user", "content": "가슴이 심하게 아프고 식은땀이 나요"}])
        self.assertTrue(answer.startswith("119"))
        self.assertIn("식은땀", answer)
        self.assertEqual(model.calls, 0)

    def test_max_tokens_is_capped_at_server_limit(self) -> None:
        class CapturingClient(LunitModelClient):
            payload: dict[str, Any] = {}

            def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
                self.payload = payload
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        client = CapturingClient(Settings(fm_api_key="test", max_tokens=9999))
        client.chat([{"role": "user", "content": "test"}])
        self.assertEqual(client.payload["max_tokens"], 2048)
        self.assertFalse(client.payload["chat_template_kwargs"]["enable_thinking"])

    def test_streamable_http_mcp_headers(self) -> None:
        seen: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                method = body["method"]
                seen.append((method, self.headers.get("Mcp-Name", "")))
                result = {"tools": [{
                    "name": "demo_search",
                    "description": "demo",
                    "inputSchema": {"type": "object", "properties": {}},
                }]} if method == "tools/list" else {"content": [{"type": "text", "text": "ok"}]}
                raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = LunitMCPClient(Settings(
                fm_api_key="test",
                mcp_url=f"http://127.0.0.1:{server.server_address[1]}/mcp",
            ))
            self.assertEqual(client.list_tools()[0]["name"], "demo_search")
            client.call_tool("demo_search", {})
            self.assertEqual(seen, [("tools/list", ""), ("tools/call", "demo_search")])
        finally:
            server.shutdown()
            server.server_close()

    def test_sse_parser(self) -> None:
        value = _parse_sse('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
        self.assertTrue(value["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
