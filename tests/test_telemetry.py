"""텔레메트리 불변식.

가장 중요한 것 하나: **관측이 답을 죽이지 않는다.** 나머지는 세는 게 맞는지 본다.
"""

from __future__ import annotations

import unittest

from telemetry import Telemetry, staged


def _response(
    content: str = "답변입니다",
    prompt: int = 100,
    completion: int = 50,
    reasoning: int | None = None,
    finish: str = "stop",
) -> dict:
    usage: dict = {"prompt_tokens": prompt, "completion_tokens": completion}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": finish}],
        "usage": usage,
    }


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = Telemetry()

    def test_counts_tokens_by_stage(self) -> None:
        self.telemetry.record("draft", _response(prompt=120, completion=60))
        self.telemetry.record("draft", _response(prompt=80, completion=40))
        self.telemetry.record("review", _response(prompt=200, completion=10))
        snapshot = self.telemetry.snapshot()
        self.assertEqual(snapshot["by_stage"]["draft"]["calls"], 2)
        self.assertEqual(snapshot["by_stage"]["draft"]["prompt_tokens_total"], 200)
        self.assertEqual(snapshot["by_stage"]["draft"]["prompt_tokens_max"], 120)
        self.assertEqual(snapshot["by_stage"]["review"]["calls"], 1)
        self.assertEqual(snapshot["total"]["calls"], 3)

    def test_flags_truncation_and_empty_answers(self) -> None:
        """이 둘이 점수에 직접 닿는다 — 잘림은 감점, 빈 답은 확정 0점이다."""
        self.telemetry.record("draft", _response(finish="length"))
        self.telemetry.record("draft", _response(content="   "))
        self.telemetry.record("draft", _response())
        stats = self.telemetry.snapshot()["by_stage"]["draft"]
        self.assertEqual(stats["truncated"], 1)
        self.assertEqual(stats["empty"], 1)
        self.assertEqual(stats["calls"], 3)

    def test_thinking_share_uses_reported_reasoning_tokens(self) -> None:
        self.telemetry.record("draft", _response(completion=1000, reasoning=480))
        self.assertEqual(self.telemetry.snapshot()["by_stage"]["draft"]["thinking_share"], 0.48)

    def test_missing_reasoning_field_is_not_guessed(self) -> None:
        """서버가 안 주면 0 이다. 문자 길이로 추정한 값이 실측처럼 보이면 더 나쁘다."""
        self.telemetry.record("draft", _response(completion=1000))
        self.assertEqual(self.telemetry.snapshot()["by_stage"]["draft"]["thinking_share"], 0.0)

    def test_never_raises_on_malformed_responses(self) -> None:
        """관측 때문에 답이 사라지면 순손실이다. 어떤 쓰레기가 와도 조용히 넘어간다."""
        for junk in (
            {}, {"usage": None}, {"choices": []}, {"choices": [None]},
            {"choices": [{"message": None}], "usage": {"prompt_tokens": "x"}},
            {"usage": {"completion_tokens_details": "not-a-dict"}},
            None, "문자열", 42, [],
        ):
            self.telemetry.record("draft", junk)  # type: ignore[arg-type]
        self.assertGreaterEqual(self.telemetry.snapshot()["total"]["calls"], 0)


class StagedTests(unittest.IsolatedAsyncioTestCase):
    async def test_labels_calls_without_touching_signatures(self) -> None:
        seen: list[dict] = []

        async def fake_call_fm(messages, max_tokens, extra=None, **kw):
            seen.append(kw)
            return _response()

        wrapped = staged(fake_call_fm, "review")
        await wrapped([{"role": "user", "content": "x"}], 100)
        self.assertEqual(seen[0]["stage"], "review")

    async def test_explicit_stage_wins_over_wrapper(self) -> None:
        seen: list[dict] = []

        async def fake_call_fm(messages, max_tokens, extra=None, **kw):
            seen.append(kw)
            return _response()

        wrapped = staged(fake_call_fm, "review")
        await wrapped([{"role": "user", "content": "x"}], 100, None, stage="digest")
        self.assertEqual(seen[0]["stage"], "digest")


if __name__ == "__main__":
    unittest.main()
