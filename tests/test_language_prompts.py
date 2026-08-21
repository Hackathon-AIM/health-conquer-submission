"""입력 언어를 보존하는 제출 프롬프트 계약."""

from __future__ import annotations

import asyncio
import unittest

import app
from generation import generation_system
from router import CLASSIFY_PROMPT, Route, classify


class GenerationLanguagePromptTest(unittest.TestCase):
    def test_enabled_english_emergency_uses_english_and_local_services(self) -> None:
        """영어 응급 답변에 한국 전용 지시가 섞이면 안 된다."""
        prompt = generation_system(
            Route(lang="en", urgency="emergency"), language_aware=True
        )

        self.assertIn("plain English", prompt)
        self.assertIn("local emergency services", prompt)
        self.assertIn("Answer in English", prompt)
        self.assertIn("Match the complexity and format requested", prompt)
        self.assertNotIn("Korean medical assistant", prompt)
        self.assertNotIn("119 / 응급실", prompt)

    def test_enabled_korean_emergency_keeps_korean_guidance(self) -> None:
        """한국어 응급 답변에서는 기존의 지역 안내를 유지한다."""
        prompt = generation_system(
            Route(lang="ko", urgency="emergency"), language_aware=True
        )

        self.assertIn("plain Korean", prompt)
        self.assertIn("119 / 응급실", prompt)
        self.assertIn("Answer in Korean", prompt)

    def test_disabled_generation_prompt_is_the_baseline(self) -> None:
        """실험을 켜지 않으면 기존 프롬프트 전문이 바뀌면 안 된다."""
        route = Route(lang="en", urgency="routine", persona="layperson")

        self.assertEqual(
            generation_system(route),
            generation_system(route, language_aware=False),
        )
        self.assertNotIn("Match the complexity and format requested", generation_system(route))


class ClassifierLanguagePromptTest(unittest.TestCase):
    def test_enabled_classifier_uses_multilingual_instruction(self) -> None:
        """영어 질문도 한국어 전제 없이 라우팅한다."""
        captured: list[list[dict]] = []

        async def fm(messages: list[dict], _tokens: int, _extra: dict) -> dict:
            captured.append(messages)
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"domain":"generic","urgency":"routine",'
                                '"context":"sufficient","ask_back":"",'
                                '"persona":"layperson","lang":"en",'
                                '"date_sensitive":false,"search_query":""}'
                            )
                        }
                    }
                ]
            }

        route = asyncio.run(
            classify(
                [{"role": "user", "content": "Please answer in English."}],
                fm,
                language_aware=True,
            )
        )

        self.assertEqual("en", route.lang)
        prompt = captured[0][0]["content"]
        self.assertIn("Korean or English", prompt)
        self.assertIn("language of the last user message", prompt)

    def test_disabled_classifier_sends_the_baseline_prompt(self) -> None:
        """기본값은 기존 분류 프롬프트를 그대로 보낸다."""
        captured: list[list[dict]] = []

        async def fm(messages: list[dict], _tokens: int, _extra: dict) -> dict:
            captured.append(messages)
            return {"choices": [{"message": {"content": "{}"}}]}

        asyncio.run(
            classify(
                [{"role": "user", "content": "Please answer in English."}],
                fm,
                language_aware=False,
            )
        )

        self.assertTrue(captured[0][0]["content"].startswith(CLASSIFY_PROMPT.split("{question}")[0]))

    def test_enabled_classifier_failure_preserves_english(self) -> None:
        """분류 5xx가 영어 답변을 한국어로 바꾸면 안 된다."""

        async def unavailable(*_args, **_kwargs) -> dict:
            raise RuntimeError("upstream unavailable")

        route = asyncio.run(
            classify(
                [{"role": "user", "content": "Please answer in English."}],
                unavailable,
                language_aware=True,
            )
        )

        self.assertEqual("en", route.lang)


class ApplicationLanguageWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_classify = app.classify
        self.original_generate = app.generate
        self.original_call_fm = app.call_fm
        self.original_language_aware = getattr(app, "LANGUAGE_AWARE_PROMPT", False)
        self.original_wait_for = app.asyncio.wait_for

    def tearDown(self) -> None:
        app.classify = self.original_classify
        app.generate = self.original_generate
        app.call_fm = self.original_call_fm
        app.LANGUAGE_AWARE_PROMPT = self.original_language_aware
        app.asyncio.wait_for = self.original_wait_for

    def test_empty_generation_retry_keeps_the_english_system_prompt(self) -> None:
        """빈 응답 재시도도 정상 생성과 같은 언어 지시를 받아야 한다."""
        seen: dict[str, object] = {}

        async def fake_classify(_messages, _call_fm, *, fallback_rules=False, language_aware=False):
            seen["classify_flag"] = language_aware
            return Route(lang="en")

        async def fake_generate(*_args, language_aware=False, **_kwargs):
            seen["generate_flag"] = language_aware
            return "", None

        async def fake_call_fm(messages, _tokens, _extra=None):
            seen["retry_messages"] = messages
            return {"choices": [{"message": {"content": "recovered"}}]}

        app.classify = fake_classify
        app.generate = fake_generate
        app.call_fm = fake_call_fm
        app.LANGUAGE_AWARE_PROMPT = True

        content = asyncio.run(
            app.generate_reply(
                [{"role": "user", "content": "Please answer in English."}],
                app.Deadline.start(120),
            )
        )

        self.assertEqual("recovered", content)
        self.assertTrue(seen["classify_flag"])
        self.assertTrue(seen["generate_flag"])
        retry_messages = seen["retry_messages"]
        self.assertEqual("system", retry_messages[0]["role"])
        self.assertIn("Answer in English", retry_messages[0]["content"])

    def test_timeout_recovery_keeps_the_english_system_prompt(self) -> None:
        """최상위 시간초과 복구도 입력 언어를 잃으면 안 된다."""
        captured: list[list[dict]] = []
        wait_calls = 0

        async def fake_call_fm(messages, _tokens, _extra=None):
            captured.append(messages)
            return {"choices": [{"message": {"content": "recovered"}}]}

        async def fake_wait_for(coro, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                coro.close()
                raise asyncio.TimeoutError
            return await coro

        app.call_fm = fake_call_fm
        app.asyncio.wait_for = fake_wait_for
        app.LANGUAGE_AWARE_PROMPT = True

        response = asyncio.run(
            app.chat_completions(
                {"messages": [{"role": "user", "content": "Please answer in English."}]}
            )
        )

        self.assertEqual("recovered", response["choices"][0]["message"]["content"])
        self.assertEqual("system", captured[0][0]["role"])
        self.assertIn("Answer in English", captured[0][0]["content"])


if __name__ == "__main__":
    unittest.main()
