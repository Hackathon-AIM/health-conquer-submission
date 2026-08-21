"""출력 검증층 회귀 테스트 — 네트워크 없이 돈다.

    python tests/test_review.py

지키려는 성질 세 가지.
  1. 깨끗한 답변에는 **FM 을 부르지 않는다.** 요청당 호출 수가 곧 지연이고,
     이 저장소에서 지연은 점수와 직결됐다.
  2. 검증층은 **어떤 경우에도 초안보다 나쁜 것을 내지 않는다.** 빈 답은 0점이다.
  3. **오탐이 없다.** 오탐 하나가 멀쩡한 답변에 FM 호출을 하나 붙인다.
     아래 오탐 회귀 2종은 실제 L2 답변에서 잡은 것이다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review as R  # noqa: E402
from budget import Deadline  # noqa: E402


class CountingFM:
    """호출 횟수를 세고, 정해둔 리뷰 결과를 돌려준다."""

    def __init__(self, verdict="ok", final="", issues=None, unanswered=None,
                 raises=None, finish_reason="stop"):
        self.calls = 0
        self.timeouts: list = []
        self.payload = {
            "verdict": verdict,
            "final": final,
            "issues": issues or [],
            "unanswered": unanswered or [],
        }
        self.raises = raises
        self.finish_reason = finish_reason

    async def __call__(self, messages, max_tokens, extra=None, timeout=None, **kw):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.raises:
            raise self.raises
        return {
            "choices": [
                {
                    "message": {"content": json.dumps(self.payload, ensure_ascii=False)},
                    "finish_reason": self.finish_reason,
                }
            ]
        }


Q = "타이레놀 성인 1회 최대 용량이 얼마인가요"
GOOD = (
    "타이레놀(아세트아미노펜) 성인 1회 최대 용량은 1,000mg 입니다. "
    "하루 총량은 4,000mg 을 넘기지 않아야 하며, 음주하거나 간 질환이 있으면 "
    "그보다 낮은 용량을 써야 합니다. 다른 감기약에도 아세트아미노펜이 들어 있는 "
    "경우가 많으니 성분을 합쳐서 계산하십시오."
)
# 하드 결함(잘림)이 있는 초안 — 리뷰를 부르는 유일한 기본 트리거다.
CUT = "타이레놀 성인 1회 최대 용량은 1,000mg 이고 하루 총량은 4,000mg 을 넘기지 않아야 하며 만약"
OFF_TOPIC = "규칙적인 운동과 균형 잡힌 식사는 전반적인 건강에 도움이 됩니다. 수면도 중요합니다."


def run(draft, question=Q, fm=None, **kw):
    fm = fm or CountingFM()
    out, notes = asyncio.run(R.review(draft, question, fm, **kw))
    return out, notes, fm


# ── 비용 ──────────────────────────────────────────────────────
def test_clean_answer_costs_zero_fm_calls() -> None:
    """가장 흔한 경로. 규칙에 안 걸리면 FM 을 안 부른다."""
    out, notes, fm = run(GOOD)
    assert fm.calls == 0, f"FM 을 {fm.calls}번 불렀다 — 0이어야 한다"
    assert out == GOOD and notes == []


def test_mode_off_is_a_passthrough() -> None:
    out, notes, fm = run("아무 말 <think>샘</think>", mode="off")
    assert out == "아무 말 <think>샘</think>"
    assert fm.calls == 0 and notes == []


def test_mode_always_calls_even_when_clean() -> None:
    fm = CountingFM(verdict="ok")
    out, notes, fm = run(GOOD, fm=fm, mode="always")
    assert fm.calls == 1
    assert out == GOOD  # verdict ok 면 초안 그대로


def test_hard_defect_triggers_the_fm_review() -> None:
    fm = CountingFM(verdict="ok")
    out, notes, fm = run(CUT, fm=fm)
    assert fm.calls == 1, "잘린 답변인데 리뷰를 안 불렀다"
    assert any("truncated" in n for n in notes), notes


# ── 오탐 회귀 (실제 L2 답변에서 잡은 것) ──────────────────────
def test_markdown_bold_tail_is_not_truncation() -> None:
    """"...바랍니다.**" 를 잘림으로 오인했었다. 굵게 표시를 닫은 별표일 뿐이다."""
    for tail in [
        "반드시 담당 산부인과 의사와 상의하여 본인에게 맞는 치료를 결정하시기 바랍니다.**",
        "위 답변은 의학적 정보 제공을 위한 것으로, 진단이나 처방을 대신할 수 없습니다.*",
        "**지금 바로 119에 연락하십시오.**",
        "시간이 심장 근육을 살리는 유일한 열쇠입니다.",
    ]:
        assert R.looks_closed(tail), f"완결인데 잘림으로 봤다: {tail[-25:]}"
        assert not any("truncated" in d for d in R.defects(tail * 3, Q))


def test_markdown_rule_is_not_repetition_collapse() -> None:
    """`--------------------` 구분선을 반복 붕괴로 오인했었다."""
    body = GOOD + "\n\n" + "-" * 40 + "\n\n참고: 개인차가 있습니다."
    d = R.defects(body, Q)
    assert not any("repeats" in x for x in d), d
    # 진짜 붕괴는 여전히 잡는다.
    assert any("repeats" in x for x in R.defects(GOOD + "!" * 40, Q))


# ── 이상 답변 탐지 ────────────────────────────────────────────
def test_detects_truncation() -> None:
    assert any("truncated" in x for x in R.defects(CUT, Q))


def test_detects_think_leak() -> None:
    body, leaked = R.strip_think("<think>사용자가 용량을 묻는다</think>1회 1,000mg 입니다.")
    assert leaked and body == "1회 1,000mg 입니다."
    body2, leaked2 = R.strip_think("답변입니다. <think>여기부터는 사고 토큰")
    assert leaked2 and body2 == "답변입니다."


def test_detects_duplicate_lines() -> None:
    dup = "\n".join(["같은 줄이 계속 반복되고 있습니다."] * 4)
    assert any("line repeats" in x for x in R.defects(dup, Q))


def test_detects_fabricated_citation() -> None:
    """근거를 하나도 안 받았으면 대괄호 번호는 전부 지어낸 것이다."""
    assert any("cites sources" in x for x in R.defects(GOOD + " 근거는 [1] 과 [2] 입니다.", Q))


def test_citation_within_evidence_range_is_fine() -> None:
    """하네스 경로는 실제로 근거를 건넨다. 그때 [1][2] 는 정상이다."""
    d = R.defects(GOOD + " 근거는 [1] 과 [2] 입니다.", Q, n_evidence=2)
    assert not any("cites sources" in x for x in d), d


def test_citation_beyond_evidence_range_is_caught() -> None:
    """근거가 2건인데 [3] 을 인용하면 그건 창작이다."""
    d = R.defects(GOOD + " 근거는 [3] 입니다.", Q, n_evidence=2)
    assert any("cites sources [3]" in x for x in d), d


def test_detects_language_mismatch() -> None:
    assert any("Korean" in x for x in R.defects(
        "The maximum single adult dose is 1,000 mg of acetaminophen.", Q))


def test_detects_deflection() -> None:
    assert any("defers" in x for x in R.defects("정확한 것은 전문가와 상담하시기 바랍니다.", Q))


# ── 적합성(커버리지) — 계산은 하되 기본은 트리거가 아니다 ─────
def test_off_topic_answer_produces_a_coverage_signal() -> None:
    gaps = R.coverage_gaps(OFF_TOPIC, Q)
    assert any("key terms" in g for g in gaps), gaps


def test_on_topic_answer_has_no_coverage_signal() -> None:
    assert R.coverage_gaps(GOOD, Q) == []


def test_multi_question_short_answer_is_flagged() -> None:
    q = "타이레놀 최대 용량이 얼마인가요? 그리고 술이랑 같이 먹어도 되나요?"
    assert any("asks" in g for g in R.coverage_gaps("1회 1,000mg 입니다.", q))


def test_coverage_alone_does_not_trigger_by_default() -> None:
    """실측 12문항에서 커버리지 신호 4건이 전부 동의어 오탐이었다.

    신호는 보고하되 FM 을 부르지는 않는다 — 오탐 하나가 곧 호출 하나다.
    """
    assert R.REVIEW_COVERAGE is False, "기본값이 켜져 있다"
    fm = CountingFM()
    out, notes, fm = run(OFF_TOPIC, fm=fm)
    assert fm.calls == 0, "커버리지 신호만으로 리뷰를 불렀다"
    assert any("key terms" in n for n in notes), "신호가 보고되지 않았다"
    assert out == OFF_TOPIC


def test_coverage_triggers_when_enabled() -> None:
    old = R.REVIEW_COVERAGE
    R.REVIEW_COVERAGE = True
    try:
        fm = CountingFM(verdict="ok")
        out, notes, fm = run(OFF_TOPIC, fm=fm)
        assert fm.calls == 1, "켰는데도 리뷰를 안 불렀다"
    finally:
        R.REVIEW_COVERAGE = old


# ── 절대 나빠지지 않는다 ──────────────────────────────────────
def test_revision_is_applied_when_better() -> None:
    fm = CountingFM(verdict="revise", final=GOOD, issues=["draft was truncated"])
    out, notes, fm = run(CUT, fm=fm)
    assert fm.calls == 1 and out == GOOD


def test_revision_that_shrinks_is_rejected() -> None:
    """요약해 버린 수정본은 초안보다 나쁘다."""
    fm = CountingFM(verdict="revise", final="1,000mg.")
    out, notes, fm = run(CUT, fm=fm)
    assert out == CUT, "요약본이 채택됐다"


def test_revision_that_fails_rules_is_rejected() -> None:
    """수정본도 규칙 검사를 통과해야 한다 — 또 잘린 수정본이면 의미가 없다."""
    fm = CountingFM(verdict="revise", final="타이레놀 성인 1회 최대 용량은 1,000mg 이고 또한 그리고 만약")
    out, notes, fm = run(CUT, fm=fm)
    assert out == CUT


def test_truncated_review_response_is_rejected() -> None:
    fm = CountingFM(verdict="revise", final=GOOD, finish_reason="length")
    out, notes, fm = run(CUT, fm=fm)
    assert out == CUT


def test_review_failure_keeps_the_draft() -> None:
    fm = CountingFM(raises=RuntimeError("상류가 죽었다"))
    out, notes, fm = run(CUT, fm=fm)
    assert out == CUT, "리뷰가 죽었는데 답이 바뀌었다"


def test_empty_draft_never_becomes_worse() -> None:
    out, notes, fm = run("   ")
    assert out == "" and fm.calls == 0


def test_review_skipped_when_out_of_time() -> None:
    """시간이 없으면 규칙 검사만 하고 내보낸다."""
    fm = CountingFM()
    spent = Deadline(total=40.0, started=time.monotonic() - 39.5)
    out, notes, fm = run(CUT, fm=fm, deadline=spent)
    assert fm.calls == 0, "시간이 없는데 리뷰를 불렀다"
    assert out == CUT and notes


def test_review_call_gets_real_time_even_when_budget_is_thin() -> None:
    """리뷰는 드물게만 돈다. 돌 때는 남은 예산이 얇아도 바닥만큼은 준다 —
    10초짜리 timeout 으로는 4096토큰 수정본을 받을 수 없어 전부 죽었다."""
    fm = CountingFM(verdict="ok")
    thin = Deadline(total=40.0, started=time.monotonic() - 30.0)  # 남은 10초
    run(CUT, fm=fm, deadline=thin)
    assert fm.calls == 1
    assert fm.timeouts[0] >= R.REVIEW_FLOOR_S, fm.timeouts[0]
    assert fm.timeouts[0] <= R.REVIEW_CAP_S


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
