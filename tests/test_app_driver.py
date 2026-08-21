from __future__ import annotations

import asyncio
import unittest
from typing import Any

import app as driver_app
from budget import Deadline


class AppDriverTests(unittest.TestCase):
    def test_generate_reply_falls_back_when_thinking_call_fails(self) -> None:
        calls: list[tuple[list[dict[str, Any]], dict[str, Any] | None]] = []

        async def fake_call_fm(
            messages: list[dict[str, Any]],
            _max_tokens: int,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            calls.append((messages, extra))
            if len(calls) == 1:
                raise RuntimeError("upstream timeout")
            return {
                "choices": [{
                    "message": {"content": "완성 답변"},
                    "finish_reason": "stop",
                }]
            }

        original = driver_app.call_fm
        driver_app.call_fm = fake_call_fm
        try:
            answer = asyncio.run(driver_app.generate_reply(
                [{"role": "user", "content": "머리가 아파요"}],
                Deadline.start(40),
            ))
        finally:
            driver_app.call_fm = original

        self.assertEqual(answer, "완성 답변")
        self.assertEqual(
            calls[0][1],
            {"chat_template_kwargs": {"enable_thinking": True}},
        )
        self.assertEqual(
            calls[1][1],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        self.assertIn(driver_app.ANSWER_INSTRUCTION, calls[1][0][-1]["content"])

    def test_generate_reply_falls_back_when_thinking_content_is_empty(self) -> None:
        calls: list[dict[str, Any] | None] = []

        async def fake_call_fm(
            _messages: list[dict[str, Any]],
            _max_tokens: int,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            calls.append(extra)
            if len(calls) == 1:
                return {
                    "choices": [{
                        "message": {"content": ""},
                        "finish_reason": "length",
                    }]
                }
            return {
                "choices": [{
                    "message": {"content": "짧은 답변"},
                    "finish_reason": "stop",
                }]
            }

        original = driver_app.call_fm
        driver_app.call_fm = fake_call_fm
        try:
            answer = asyncio.run(driver_app.generate_reply(
                [{"role": "user", "content": "감기약 먹어도 되나요?"}],
                Deadline.start(40),
            ))
        finally:
            driver_app.call_fm = original

        self.assertEqual(answer, "짧은 답변")
        self.assertEqual(
            calls,
            [
                {"chat_template_kwargs": {"enable_thinking": True}},
                {"chat_template_kwargs": {"enable_thinking": False}},
            ],
        )

    def test_answer_instruction_handles_multimodal_content_lists(self) -> None:
        messages = driver_app._messages_with_answer_instruction([
            {"role": "user", "content": [{"type": "text", "text": "질문"}]},
        ])

        self.assertEqual(messages[0]["content"][-1]["type"], "text")
        self.assertIn(driver_app.ANSWER_INSTRUCTION, messages[0]["content"][-1]["text"])

    def test_chat_completion_never_returns_empty_content(self) -> None:
        async def fake_generate_reply(
            _messages: list[dict[str, Any]],
            _deadline: Deadline,
        ) -> str:
            return ""

        original = driver_app.generate_reply
        driver_app.generate_reply = fake_generate_reply
        try:
            response = asyncio.run(driver_app.chat_completions({
                "messages": [{"role": "user", "content": "가슴이 답답해요"}],
            }))
        finally:
            driver_app.generate_reply = original

        content = response["choices"][0]["message"]["content"]
        self.assertTrue(content)
        self.assertIn("119", content)

    def test_mcp_gate_ignores_ordinary_symptom_question(self) -> None:
        route = driver_app._mcp_route([
            {"role": "user", "content": "감기 기운이 있는데 물 많이 마셔도 되나요?"},
        ])

        self.assertIsNone(route)

    def test_mcp_gate_routes_explicit_evidence_request(self) -> None:
        route = driver_app._mcp_route([
            {"role": "user", "content": "커피가 심방세동 위험을 높인다는 연구 근거가 진짜 있나요?"},
        ])

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.domain, "pubmed")
        self.assertIn("rag_vector_query", route.tools)

    def test_mcp_gate_routes_policy_code_and_specific_drug_questions(self) -> None:
        cases = [
            ("KCD 질병코드 F32는 어떤 의미야?", "kcd"),
            ("이 처치가 급여 청구 가능한지 심평원 고시 기준 알려줘", "hira_updates"),
            ("세툭시맙 부작용에 변비가 있는지 허가사항 근거로 알려줘", "adr"),
        ]

        for question, domain in cases:
            with self.subTest(question=question):
                route = driver_app._mcp_route([{"role": "user", "content": question}])
                self.assertIsNotNone(route)
                assert route is not None
                self.assertEqual(route.domain, domain)
                self.assertTrue(route.tools)

    def test_mcp_gate_routes_english_external_evidence_questions(self) -> None:
        cases = [
            ("Is there peer-reviewed evidence that coffee raises atrial fibrillation risk?", "pubmed"),
            ("Is this procedure covered by HIRA reimbursement rules?", "hira_updates"),
            ("What does ICD code F32 mean?", "kcd"),
            ("Can I take ibuprofen with warfarin? Check the official label.", "mfds"),
            ("Is cetuximab contraindicated in pregnancy according to the drug label?", "mfds"),
            ("Which article of Korean law creates this reporting duty?", "korean_law"),
        ]

        for question, domain in cases:
            with self.subTest(question=question):
                route = driver_app._mcp_route([{"role": "user", "content": question}])
                self.assertIsNotNone(route)
                assert route is not None
                self.assertEqual(route.domain, domain)
                self.assertEqual(route.lang, "en")
                self.assertTrue(route.tools)

    def test_mcp_gate_ignores_ordinary_english_symptom_question(self) -> None:
        route = driver_app._mcp_route([
            {"role": "user", "content": "I have a mild sore throat. Should I drink warm water?"},
        ])

        self.assertIsNone(route)

    def test_optional_mcp_uses_direct_path_for_non_gate_question(self) -> None:
        async def fake_generate_reply(
            _messages: list[dict[str, Any]],
            _deadline: Deadline,
        ) -> str:
            return "직접 답변"

        async def fail_generate(*_args, **_kwargs) -> tuple[str, Any]:
            raise AssertionError("MCP path should not run")

        original_reply = driver_app.generate_reply
        original_generate = driver_app.generate
        driver_app.generate_reply = fake_generate_reply
        driver_app.generate = fail_generate
        try:
            answer = asyncio.run(driver_app.answer_with_optional_mcp(
                [{"role": "user", "content": "목이 조금 따가워요"}],
                Deadline.start(40),
            ))
        finally:
            driver_app.generate_reply = original_reply
            driver_app.generate = original_generate

        self.assertEqual(answer, "직접 답변")

    def test_optional_mcp_falls_back_to_direct_when_mcp_path_fails(self) -> None:
        async def fail_generate(*_args, **_kwargs) -> tuple[str, Any]:
            raise RuntimeError("mcp down")

        async def fake_generate_reply(
            _messages: list[dict[str, Any]],
            _deadline: Deadline,
        ) -> str:
            return "직접 복구 답변"

        original_generate = driver_app.generate
        original_reply = driver_app.generate_reply
        driver_app.generate = fail_generate
        driver_app.generate_reply = fake_generate_reply
        try:
            answer = asyncio.run(driver_app.answer_with_optional_mcp(
                [{"role": "user", "content": "아스피린과 와파린 병용 금기 근거 알려줘"}],
                Deadline.start(40),
            ))
        finally:
            driver_app.generate = original_generate
            driver_app.generate_reply = original_reply

        self.assertEqual(answer, "직접 복구 답변")


if __name__ == "__main__":
    unittest.main()
