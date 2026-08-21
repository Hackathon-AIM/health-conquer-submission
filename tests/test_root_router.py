"""제출 Docker 라우터의 상류 실패 폴백 회귀 테스트."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from router import classify


async def unavailable_classifier(*_args, **_kwargs):
    raise RuntimeError("upstream unavailable")


class RouterFallbackTest(unittest.TestCase):
    def test_guideline_question_keeps_retrieval_after_classifier_failure(self) -> None:
        """분류 모델이 죽어도 명시적 지침 질문을 generic으로 내려보내면 안 된다."""
        route = asyncio.run(
            classify(
                [
                    {
                        "role": "user",
                        "content": (
                            "According to clinical guidelines, what blood pressure target is "
                            "recommended for adults with chronic kidney disease?"
                        ),
                    }
                ],
                unavailable_classifier,
                fallback_rules=True,
            )
        )

        self.assertEqual(route.source, "fallback")
        self.assertEqual(route.domain, "guideline_index")
        self.assertTrue(route.tools)
        self.assertEqual(route.lang, "en")

    def test_english_generic_question_keeps_english_in_fallback(self) -> None:
        """분류 실패가 영어 질문을 한국어 시스템 지시로 바꾸면 언어 축을 잃는다."""
        route = asyncio.run(
            classify(
                [{"role": "user", "content": "What are common symptoms of dehydration?"}],
                unavailable_classifier,
                fallback_rules=True,
            )
        )

        self.assertEqual(route.source, "fallback")
        self.assertEqual(route.domain, "generic")
        self.assertEqual(route.lang, "en")


if __name__ == "__main__":
    unittest.main()
