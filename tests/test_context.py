"""소형 컨텍스트 계층의 불변식.

핵심 불변식 하나: **툴 결과가 얼마나 크든, 프록시를 지난 뒤에는 예산을 넘지 않는다.**
나머지 테스트는 그 불변식을 지키는 과정에서 답이 망가지지 않는지 본다.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from submission.config import Settings
from submission.context import ContextBudget, TokenCounter, fit_messages
from submission.orchestrator import NativeDriver, _auto_arguments
from submission.rag import SessionIndex, chunk_text
from submission.reduce import extract_documents, strip_noise
from submission.toolspec import build_tools, minify, rank_tool_names


def _huge_mcp_result(documents: int = 40, paragraphs: int = 25) -> dict[str, Any]:
    """실제로 본 적 있는 모양의 대용량 결과 — 본문이 EmbeddedResource 안에 숨어 있다."""
    payload = {
        "results": [
            {
                "cite_uid": f"guideline:{index}",
                "title": f"고혈압 진료지침 {index}장",
                "url": f"https://example.org/doc/{index}?utm_source=mcp&utm_campaign=x",
                "content": "\n\n".join(
                    f"{index}-{p} 성인 고혈압 환자에서 목표 혈압은 수축기 130 mmHg 미만이다. "
                    "이뇨제와 병용 시 저칼륨혈증을 주기적으로 확인한다. " * 3
                    for p in range(paragraphs)
                ),
            }
            for index in range(documents)
        ]
    }
    return {
        "content": [
            {"type": "text", "text": "search completed: 40 documents"},
            {"type": "resource", "resource": {
                "uri": "index://search/1",
                "mimeType": "application/json",
                "text": json.dumps(payload, ensure_ascii=False),
            }},
        ]
    }


class TokenCounterTests(unittest.TestCase):
    def test_korean_costs_more_per_char_than_english(self) -> None:
        counter = TokenCounter()
        korean = counter.estimate_text("가" * 100)
        english = counter.estimate_text("a" * 100)
        self.assertGreater(korean, english)

    def test_truncate_respects_token_budget(self) -> None:
        counter = TokenCounter()
        text = "아스피린과 와파린의 병용은 출혈 위험을 높인다. " * 50
        for limit in (10, 40, 120):
            self.assertLessEqual(counter.estimate_text(counter.truncate(text, limit)), limit)

    def test_truncate_middle_keeps_both_ends(self) -> None:
        counter = TokenCounter()
        text = "시작지점입니다. " + "중간 " * 200 + "끝지점입니다."
        shortened = counter.truncate_middle(text, 60)
        self.assertTrue(shortened.startswith("시작"))
        self.assertTrue(shortened.endswith("끝지점입니다."))
        self.assertLessEqual(counter.estimate_text(shortened), 60)

    def test_calibration_moves_toward_observed_truth(self) -> None:
        counter = TokenCounter()
        before = counter.estimate_text("가나다라마바사")
        for _ in range(6):
            estimated = counter.estimate_text("가나다라마바사")
            counter.observe(estimated, estimated * 2)  # 서버가 우리 추정의 2배라고 알려 준다
        self.assertGreater(counter.estimate_text("가나다라마바사"), before)


class BudgetTests(unittest.TestCase):
    def test_allocation_never_exceeds_total(self) -> None:
        for total in (256, 512, 1024, 2560, 8192, 32768):
            budget = ContextBudget.allocate(total)
            parts = budget.system + budget.conversation + budget.evidence + budget.tools
            self.assertLessEqual(parts + budget.reserve, budget.total, f"total={total}")
            self.assertGreater(budget.evidence, 0, f"total={total}")

    def test_fit_keeps_system_and_last_turn(self) -> None:
        counter = TokenCounter()
        messages = [{"role": "system", "content": "지침"}]
        messages += [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}번째 " + "내용 " * 60}
            for i in range(12)
        ]
        messages.append({"role": "user", "content": "마지막 질문입니다"})
        fitted, dropped = fit_messages(messages, 300, counter)
        self.assertEqual(fitted[0]["role"], "system")
        self.assertEqual(fitted[-1]["content"], "마지막 질문입니다")
        self.assertGreater(dropped, 0)
        self.assertLessEqual(counter.estimate_messages(fitted), 300)

    def test_fit_never_orphans_tool_messages(self) -> None:
        """assistant(tool_calls) 없이 tool 메시지만 남으면 상류가 400 을 던진다."""
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "옛날 질문 " * 40},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "결과 " * 40},
            {"role": "user", "content": "지금 질문"},
        ]
        fitted, _ = fit_messages(messages, 120, counter)
        for index, message in enumerate(fitted):
            if message.get("role") == "tool":
                self.assertTrue(
                    any(m.get("tool_calls") for m in fitted[:index]),
                    "tool 메시지가 짝 없이 남았다",
                )

    def test_single_oversized_turn_is_shrunk_not_dropped(self) -> None:
        counter = TokenCounter()
        messages = [{"role": "user", "content": "질문 " * 2000}]
        fitted, _ = fit_messages(messages, 150, counter)
        self.assertEqual(len(fitted), 1)
        self.assertLessEqual(counter.estimate_messages(fitted), 150)


class ReduceTests(unittest.TestCase):
    def test_strip_noise_removes_blobs_and_tracking(self) -> None:
        text = "본문 " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVph" * 8 + " https://x.org/a?utm_source=y&z=1 끝"
        cleaned = strip_noise(text)
        self.assertIn("[blob]", cleaned)
        self.assertIn("https://x.org/a", cleaned)
        self.assertNotIn("utm_source", cleaned)
        self.assertLess(len(cleaned), len(text))

    def test_extracts_body_hidden_in_embedded_resource(self) -> None:
        documents = extract_documents("index_keyword_search", _huge_mcp_result(documents=3, paragraphs=2))
        uids = {document.cite_uid for document in documents}
        self.assertIn("guideline:0", uids)
        self.assertTrue(any("수축기 130" in document.text for document in documents))

    def test_plain_text_result_still_yields_a_document(self) -> None:
        result = {"content": [{"type": "text", "text": "메트포르민은 신기능 저하 시 감량한다."}]}
        documents = extract_documents("adr_retrieve_drug_info", result)
        self.assertEqual(len(documents), 1)
        self.assertTrue(documents[0].cite_uid)


class SessionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = TokenCounter()

    def test_chunks_stay_within_target(self) -> None:
        text = "고혈압 환자의 목표 혈압은 130 미만이다. " * 200
        for target in (64, 128, 256):
            for piece in chunk_text(text, self.counter, target):
                self.assertLessEqual(self.counter.estimate_text(piece), int(target * 1.35))

    def test_finds_the_needle_in_a_large_result(self) -> None:
        index = SessionIndex(self.counter, chunk_tokens=120)
        index.add_documents(extract_documents("index_keyword_search", _huge_mcp_result()))
        index.add_documents(extract_documents("adr_retrieve_drug_info", {
            "content": [{"type": "text", "text": json.dumps({
                "cite_uid": "dailymed:warfarin",
                "title": "와파린 라벨",
                "content": "와파린과 아스피린을 병용하면 출혈 위험이 유의하게 증가한다.",
            }, ensure_ascii=False)}],
        }))
        picked = index.select("와파린과 아스피린을 같이 먹어도 되나요", token_budget=300, top_k=3)
        self.assertTrue(picked)
        self.assertEqual(picked[0].cite_uid, "dailymed:warfarin")

    def test_selection_respects_token_budget_and_diversity(self) -> None:
        index = SessionIndex(self.counter, chunk_tokens=100)
        index.add_documents(extract_documents("index_keyword_search", _huge_mcp_result()))
        picked = index.select("고혈압 목표 혈압", token_budget=250, top_k=5, max_per_document=2)
        self.assertLessEqual(sum(chunk.tokens for chunk in picked), 250)
        for uid in {chunk.cite_uid for chunk in picked}:
            self.assertLessEqual(sum(1 for chunk in picked if chunk.cite_uid == uid), 2)

    def test_korean_josa_does_not_break_matching(self) -> None:
        index = SessionIndex(self.counter, chunk_tokens=80)
        index.add_documents(extract_documents("t", {"content": [{"type": "text", "text": json.dumps({
            "cite_uid": "d1", "content": "메트포르민은 조영제 사용 전 일시 중단한다."}, ensure_ascii=False)}]}))
        # 질문은 '메트포르민을', 문서는 '메트포르민은' — 어절은 다르지만 같은 약이다.
        self.assertTrue(index.select("메트포르민을 조영제와", token_budget=200, top_k=2))


class ToolSpecTests(unittest.TestCase):
    def test_ranking_routes_by_topic(self) -> None:
        self.assertIn("openapi_law_search", rank_tool_names("의료법 시행령 조문 알려줘")[:3])
        self.assertIn("openapi_hira_get_drug_price", rank_tool_names("이 약 급여 되나요 약가")[:4])
        self.assertIn("kcd_search_codes", rank_tool_names("당뇨 상병코드 KCD")[:3])

    def test_minify_shrinks_schema(self) -> None:
        counter = TokenCounter()
        definition = {
            "name": "index_keyword_search",
            "description": "아주 긴 설명. " * 40,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "설명 " * 30},
                    "page": {"type": "integer", "description": "페이지 " * 30},
                    "mode": {"type": "string", "enum": [f"m{i}" for i in range(40)]},
                },
                "required": ["query"],
            },
        }
        full = counter.estimate_text(json.dumps(minify(definition, counter, level=0), ensure_ascii=False))
        tight = counter.estimate_text(json.dumps(minify(definition, counter, level=2), ensure_ascii=False))
        self.assertLess(tight, full)
        skeleton = minify(definition, counter, level=2)
        self.assertEqual(list(skeleton["function"]["parameters"]["properties"]), ["query"])

    def test_tools_fit_the_budget(self) -> None:
        counter = TokenCounter()
        definitions = [
            {
                "name": name,
                "description": "설명입니다. " * 30,
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "질의 " * 20}},
                    "required": ["query"],
                },
            }
            for name in ("adr_retrieve_drug_info", "rag_vector_query", "index_keyword_search",
                         "index_get_relevant_nodes", "openapi_law_search")
        ]
        for token_budget in (120, 250, 600):
            tools, _ = build_tools(definitions, "약 부작용", counter, token_budget=token_budget)
            cost = counter.estimate_text(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
            self.assertLessEqual(cost, token_budget, f"budget={token_budget}")


class AutoArgumentTests(unittest.TestCase):
    def test_fills_query_slot(self) -> None:
        definition = {"inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
        self.assertEqual(_auto_arguments(definition, "와파린"), {"query": "와파린"})

    def test_skips_tools_needing_unguessable_ids(self) -> None:
        definition = {"inputSchema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}, "page": {"type": "integer"}},
            "required": ["document_id", "page"],
        }}
        self.assertIsNone(_auto_arguments(definition, "고혈압"))


class EndToEndBudgetTests(unittest.TestCase):
    """프록시를 지난 뒤에는 어떤 결과도 예산을 넘지 않는다 — 이게 전체의 합격 기준이다."""

    def test_receipt_and_evidence_stay_inside_budget(self) -> None:
        settings = Settings(fm_api_key="test", max_input_tokens=2560)
        driver = NativeDriver.__new__(NativeDriver)
        driver.settings = settings
        budget = driver._budget()
        counter = TokenCounter()

        index = SessionIndex(counter, chunk_tokens=NativeDriver._chunk_tokens(budget))
        observed: dict[str, Any] = {}
        raw = _huge_mcp_result()
        raw_tokens = counter.estimate_text(json.dumps(raw, ensure_ascii=False))
        NativeDriver._absorb("index_keyword_search", raw, index, observed)

        receipt = driver._receipt(raw, index, "고혈압 목표 혈압은 얼마인가요", budget)
        self.assertLess(counter.estimate_text(receipt), budget.evidence)
        # 실제로 큰 결과여야 이 테스트가 의미가 있다.
        self.assertGreater(raw_tokens, 20_000)
        self.assertLess(counter.estimate_text(receipt) * 20, raw_tokens)

    def test_render_fits_evidence_budget_for_any_result_size(self) -> None:
        from submission.orchestrator import CitationSelection

        settings = Settings(fm_api_key="test", max_input_tokens=2560)
        driver = NativeDriver.__new__(NativeDriver)
        driver.settings = settings
        budget = driver._budget()
        counter = TokenCounter()
        for documents in (1, 10, 60):
            index = SessionIndex(counter, chunk_tokens=NativeDriver._chunk_tokens(budget))
            observed: dict[str, Any] = {}
            NativeDriver._absorb("index_keyword_search", _huge_mcp_result(documents), index, observed)
            selection = CitationSelection(
                "partial", [], "", observed, "finalized", index, "고혈압 목표 혈압",
            )
            rendered = driver._render(selection)
            self.assertLessEqual(
                counter.estimate_text(rendered), budget.evidence + 40, f"documents={documents}"
            )


class _RecordingModel:
    """모델에 실제로 나가는 모든 요청을 붙잡아 두고, 실제로 제공된 툴만 호출한다.

    스크립트에 툴 이름을 박아 두면 예산이 바뀌어 그 툴이 안 실렸을 때 조용히 allowlist
    거절로 빠진다. 그건 코드가 아니라 테스트가 틀린 것이므로, 여기서는 그때그때 제공된
    툴 중 하나를 고른다 — 실제 모델이 하는 일과 같다.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[list[dict[str, Any]], Any]] = []
        self.observed_uids: list[str] = []

    def chat(self, messages, *, tools=None, tool_choice=None, temperature=0.1):
        from submission.model import ChatResponse

        self.requests.append(([dict(m) for m in messages], tools))
        names = [t["function"]["name"] for t in (tools or [])]
        offered = [name for name in names if name != "finalize_retrieval"]

        for message in messages:
            if message.get("role") == "tool":
                for line in str(message.get("content") or "").splitlines():
                    if line.startswith("- "):
                        self.observed_uids.append(line[2:].split("|")[0].strip())

        called = any(m.get("role") == "tool" for m in messages)
        if offered and not called:
            message = {"role": "assistant", "content": None, "tool_calls": [
                _tool_call("r1", offered[0], {"query": "고혈압 목표 혈압과 이뇨제 부작용"})]}
        elif "finalize_retrieval" in names:
            message = {"role": "assistant", "content": None, "tool_calls": [
                _tool_call("r2", "finalize_retrieval", {
                    "status": "sufficient", "items": self.observed_uids[:2]})]}
        else:
            message = {"role": "assistant", "content": "근거를 반영한 답변입니다 [1]"}
        return ChatResponse(message=message, raw={"choices": [{"message": message}]})


class _HugeMCP:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": "매우 긴 도구 설명입니다. " * 25,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "검색 질의 " * 15},
                        "top_k": {"type": "integer", "description": "개수 " * 15},
                    },
                    "required": ["query"],
                },
            }
            for name in (
                "index_get_relevant_nodes", "index_keyword_search", "rag_vector_query",
                "adr_retrieve_drug_info", "openapi_law_search", "kcd_search_codes",
                "hira_updates_search", "openapi_hira_get_drug_price",
            )
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(name)
        return _huge_mcp_result()


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class DriverBudgetTests(unittest.TestCase):
    """드라이버가 실제로 보내는 모든 요청이 창 안에 들어가는지 본다.

    앞의 단위 테스트들이 각 부품을 보는 것이라면, 이건 합쳐 놓았을 때도 성립하는지를 본다.
    부품이 다 예산을 지켜도 합이 넘으면 소용없다.
    """

    def _run(self, max_input_tokens: int) -> tuple[_RecordingModel, _HugeMCP, str]:
        settings = Settings(
            fm_api_key="test",
            max_input_tokens=max_input_tokens,
            max_retrieval_steps=3,
            max_tool_calls=2,
            max_retrieval_calls=1,
        )
        model = _RecordingModel()
        mcp = _HugeMCP()
        driver = NativeDriver(settings, model=model, mcp=mcp)
        # 되묻기 게이트에 걸리면 모델을 아예 안 부르므로, 약을 특정해 그 경로를 피한다.
        conversation = [
            {"role": "user", "content": "안녕하세요 암로디핀 5mg 을 복용 중입니다 " + "이전 맥락 " * 80},
            {"role": "assistant", "content": "네, 말씀해 주세요. " + "이전 답변 " * 80},
            {"role": "user", "content": (
                "여기에 이뇨제를 추가하면 저칼륨혈증 같은 부작용이 생기는지 최신 가이드라인 "
                "근거로 알려주세요. 목표 혈압 수치도 함께 알려주시면 좋겠습니다."
            )},
        ]
        return model, mcp, driver.answer(conversation)

    def test_requests_stay_within_window_at_every_budget(self) -> None:
        for total in (768, 1024, 2560, 8192):
            model, _mcp, answer = self._run(total)
            budget = ContextBudget.allocate(total)
            self.assertTrue(model.requests, f"total={total}: 모델 호출이 없었다")
            for index, (messages, tools) in enumerate(model.requests):
                used = TokenCounter().estimate_request(messages, tools)
                self.assertLessEqual(
                    used, budget.total,
                    f"total={total} 요청 #{index} 가 {used} tok 으로 창을 넘었다",
                )
            self.assertTrue(answer)

    def test_huge_tool_output_never_reaches_the_model(self) -> None:
        """371K 토큰짜리 결과를 돌려주는 MCP 를 붙여도 모델이 보는 건 영수증뿐이다."""
        model, mcp, _answer = self._run(2560)
        budget = ContextBudget.allocate(2560)
        counter = TokenCounter()
        self.assertTrue(mcp.calls, "MCP 가 호출되지 않았다 — 이 테스트가 무의미해진다")
        raw_tokens = counter.estimate_text(json.dumps(_huge_mcp_result(), ensure_ascii=False))
        biggest = 0
        for messages, _tools in model.requests:
            for message in messages:
                content = message.get("content") or ""
                if message.get("role") == "tool":
                    biggest = max(biggest, counter.estimate_text(content))
                # 원문 JSON 이 그대로 실렸다면 걷어내지 않은 추적 파라미터가 남아 있다.
                self.assertNotIn("utm_source", content)
        self.assertLessEqual(biggest, budget.evidence + 40)
        self.assertGreater(raw_tokens / max(1, biggest), 100, "절감이 100배에 못 미친다")

    def test_answer_survives_when_no_tool_fits_the_window(self) -> None:
        """툴 스키마 하나도 못 싣는 창에서도 답은 나와야 한다 (규칙 라우팅 경로)."""
        model, mcp, answer = self._run(320)
        self.assertTrue(answer)
        self.assertTrue(mcp.calls, "규칙 라우팅이 MCP 를 직접 부르지 않았다")


if __name__ == "__main__":
    unittest.main()
