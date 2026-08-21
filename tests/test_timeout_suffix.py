"""바깥 timeout 폴백도 anti-refusal suffix 를 유지해야 한다.

챔피언 c34d87b 는 timeout 폴백에서 원본 `messages` 를 그대로 다시 보냈다.
그래서 60초를 넘긴 문항만 조용히 지시를 잃었다 — 로그로도 드러나지 않는다.
"""

from __future__ import annotations

import asyncio
import unittest

import app


class TimeoutFallbackKeepsSuffix(unittest.TestCase):
    def setUp(self) -> None:
        self._call_fm = app.call_fm
        self._budget = app.REQUEST_BUDGET_S

    def tearDown(self) -> None:
        app.call_fm = self._call_fm
        app.REQUEST_BUDGET_S = self._budget

    def test_retry_carries_instruction_and_leaves_caller_untouched(self) -> None:
        seen: list[list[dict]] = []

        async def fake_call_fm(messages, max_tokens, extra=None):
            seen.append([dict(m) for m in messages])
            if len(seen) == 1:
                # 바깥 wait_for 가 먼저 만료되게 한다.
                await asyncio.sleep(5)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        app.call_fm = fake_call_fm
        # timeout = REQUEST_BUDGET_S + 20 → 0.5초
        app.REQUEST_BUDGET_S = -19.5

        original = [{"role": "user", "content": "가슴이 답답합니다"}]
        result = asyncio.run(app.chat_completions({"messages": original}))

        # 1) 바깥 timeout 이 실제로 발생해 재시도가 일어났다
        self.assertEqual(len(seen), 2, "timeout 폴백이 호출되지 않았다")

        # 2) 재시도 요청에도 suffix 가 실려 있다
        retry_last = seen[1][-1]["content"]
        self.assertIn(app.ANSWER_INSTRUCTION, retry_last)

        # 3) evaluator 가 넘긴 원본 객체는 변형되지 않았다
        self.assertEqual(original[-1]["content"], "가슴이 답답합니다")
        self.assertNotIn(app.ANSWER_INSTRUCTION, original[-1]["content"])

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")


if __name__ == "__main__":
    unittest.main()
