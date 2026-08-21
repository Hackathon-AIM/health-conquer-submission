"""다이제스트 회귀 테스트 — 네트워크 없이 돈다.

    python tests/test_digest.py

지키려는 것
  1. **기본은 꺼짐이고, 켜도 웬만하면 안 돈다.** 요청당 호출 수가 곧 지연이고
     이 저장소에서 지연은 점수와 직결됐다. 추출로 충분하면 호출 0회여야 한다.
  2. 돌 때는 **병렬 한 라운드**다. 순차 refine 이면 청크 수만큼 지연이 쌓인다.
  3. 호출 수에 상한이 있다. 100페이지가 오면 호출이 십수 개로 늘어서는 안 된다.
  4. 어떤 실패에서도 **추출 결과보다 나쁜 것을 내지 않는다.**
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import digest as D  # noqa: E402
from budget import Deadline  # noqa: E402


@dataclass
class Ev:
    text: str
    relevance: float = 0.0
    title: str = ""
    url: str = ""
    source_type: str = ""


Q = "아세트아미노펜 성인 1회 최대 용량"
# 질의어와 고르게 겹치는 큰 덩어리 — 문단 선택이 잘 안 듣는 모양이다.
BULK = "\n\n".join(
    f"아세트아미노펜 성인 용량에 관한 설명 {i} 번째 문단이며 임상 상황을 길게 서술한다. " * 4
    for i in range(200)
)
# DIGEST_MIN_CHARS 를 넘겨야 크기 게이트를 통과한다 — 안 넘으면 이 파일의
# '돌 때' 테스트들이 조용히 무의미해진다.
assert len(BULK) > 20000, len(BULK)


class FakeFM:
    def __init__(self, reply="1회 1,000mg, 1일 4,000mg 상한.", delay=0.05, raises=None):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.reply = reply
        self.delay = delay
        self.raises = raises

    async def __call__(self, messages, max_tokens, extra=None, timeout=None, **kw):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            if self.raises:
                raise self.raises
            return {"choices": [{"message": {"content": self.reply}, "finish_reason": "stop"}]}
        finally:
            self.concurrent -= 1


def run(items, fm, budget=4000, deadline=None, mode="auto", strategy=None):
    old, olds = D.DIGEST_MODE, D.DIGEST_STRATEGY
    D.DIGEST_MODE = mode
    if strategy:
        D.DIGEST_STRATEGY = strategy
    try:
        return asyncio.run(D.digest(items, Q, budget, fm, deadline=deadline))
    finally:
        D.DIGEST_MODE, D.DIGEST_STRATEGY = old, olds


# ── 비용: 웬만하면 안 돈다 ────────────────────────────────────
def test_off_by_default() -> None:
    assert D.DIGEST_MODE == "off", "기본값이 켜져 있다"


def test_small_evidence_costs_zero_calls() -> None:
    fm = FakeFM()
    packed, calls = run([Ev(text="아세트아미노펜 1회 1,000mg 상한.")], fm)
    assert calls == 0 and fm.calls == 0, "작은 근거인데 요약을 불렀다"
    assert packed and "1,000mg" in packed[0][1]


def test_source_below_the_size_threshold_costs_zero_calls() -> None:
    """작은 근거는 추출로 충분하다. 호출 하나가 곧 지연이다."""
    para = "요양급여 적정성 평가에 대한 배경 설명이다. " * 10
    items = [Ev(text="\n\n".join([para] * 5))]
    assert sum(len(i.text) for i in items) < D.DIGEST_MIN_CHARS
    fm = FakeFM()
    packed, calls = run(items, fm, budget=4000)
    assert calls == 0, f"불필요하게 {calls}회 불렀다"


def test_source_within_budget_reach_costs_zero_calls() -> None:
    """예산으로 감당되는 크기면 추출이 이긴다 — 원문 그대로이고 공짜다.

    실측: 원문 27,031자에 예산 12,000자를 주면 추출만으로 PSA·Gleason·
    10 ng/mL 이 전부 살아남는다. 같은 조건에서 요약은 호출 9회에 9.9초다.
    """
    fm = FakeFM()
    packed, calls = run([Ev(text=BULK)], fm, budget=len(BULK), deadline=Deadline.start(120.0))
    assert calls == 0, f"예산으로 감당되는데 {calls}회 불렀다"


def test_skips_when_out_of_time() -> None:
    fm = FakeFM()
    spent = Deadline(total=40.0, started=time.monotonic() - 35.0)  # 남은 5초
    packed, calls = run([Ev(text=BULK)], fm, deadline=spent)
    assert calls == 0, "시간이 없는데 요약을 돌렸다"
    assert packed, "추출 결과마저 없다"


# ── 돌 때: 병렬 한 라운드 ─────────────────────────────────────
def test_runs_in_parallel_not_sequentially() -> None:
    """순차면 청크 수만큼 지연이 쌓인다. 다만 무제한 병렬도 안 된다 —

    동시성에 상한이 없으면 우리 요청 하나가 상류를 밀어낸다. 그래서 파도로 돈다:
    벽시계 ≈ (청크수 ÷ 동시성) × 호출 1회.
    """
    fm = FakeFM(delay=0.05)
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(60.0))
    assert calls >= 2, f"요약이 안 돌았다 (calls={calls})"
    assert fm.max_concurrent > 1, "직렬로 돌았다"
    assert fm.max_concurrent <= D.DIGEST_CONCURRENCY, (
        f"동시 {fm.max_concurrent} > 상한 {D.DIGEST_CONCURRENCY}"
    )


def test_covers_every_chunk_not_just_the_first_few() -> None:
    """예전 상한 4청크는 27,031자 문서의 80% 만 덮었다.

    못 덮은 조각은 추출식으로 떨어져 대부분 버려졌다 — 그게 이 파일의 요지다.
    """
    n_chunks = len(D.chunks(BULK, D.DIGEST_CHUNK_CHARS))
    fm = FakeFM()
    packed, calls = run([Ev(text=BULK)], fm, budget=12000, deadline=Deadline.start(120.0))
    assert calls == min(n_chunks, D.DIGEST_MAX_CALLS), (
        f"{n_chunks}조각 중 {calls}개만 요약했다"
    )


def test_summary_stays_within_budget() -> None:
    """조각마다 요약을 받으면 합계가 예산을 넘을 수 있다 — 실측에서 23,200자가 나왔다."""
    fm = FakeFM(reply="사실 한 줄. " * 120)
    packed, calls = run([Ev(text=BULK)], fm, budget=3000, deadline=Deadline.start(120.0))
    assert sum(len(t) for _, t in packed) <= 3300, sum(len(t) for _, t in packed)


def test_call_count_is_capped() -> None:
    """100페이지가 와도 호출이 십수 개로 늘면 안 된다."""
    fm = FakeFM()
    huge = Ev(text="\n\n".join([BULK] * 5))
    packed, calls = run([huge], fm, deadline=Deadline.start(40.0))
    assert calls <= D.DIGEST_MAX_CALLS, f"{calls}회 불렀다 (상한 {D.DIGEST_MAX_CALLS})"


def test_summary_replaces_the_bulk() -> None:
    fm = FakeFM(reply="성인 1회 최대 1,000mg. 1일 4,000mg.")
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(40.0))
    assert calls > 0
    text = packed[0][1]
    assert "1,000mg" in text, text[:200]
    assert len(text) < 2000, len(text)


# ── 실패해도 추출본보다 나빠지지 않는다 ───────────────────────
def test_all_calls_failing_falls_back_to_extraction() -> None:
    fm = FakeFM(raises=RuntimeError("상류가 죽었다"))
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(40.0))
    assert packed, "근거가 통째로 사라졌다"
    assert len(packed[0][1]) > 100, "추출본이 비어 있다"


def test_none_replies_fall_back_to_extraction() -> None:
    """관련 없다고 답한 청크만 있으면 요약본이 비는데, 그때 근거를 잃으면 안 된다."""
    fm = FakeFM(reply="NONE")
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(40.0))
    assert calls > 0
    assert packed and len(packed[0][1]) > 100, "NONE 만 받고 근거를 버렸다"


def test_result_stays_within_budget() -> None:
    fm = FakeFM(reply="요약 " * 500)  # 요약본이 예산을 넘는 경우
    packed, calls = run([Ev(text=BULK)], fm, budget=1000, deadline=Deadline.start(40.0))
    assert sum(len(t) for _, t in packed) <= 1200, sum(len(t) for _, t in packed)


# ── refine 전략 (순차 누적) ───────────────────────────────────
def test_map_is_the_default_strategy() -> None:
    assert D.DIGEST_STRATEGY == "map", "기본 전략이 map 이 아니다"


def test_refine_runs_sequentially() -> None:
    """refine 은 앞 요약을 봐야 하므로 직렬이어야 한다. 병렬이면 그건 map 이다."""
    fm = FakeFM(delay=0.05)
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(60.0), strategy="refine")
    assert calls >= 2, f"refine 이 안 돌았다 (calls={calls})"
    assert fm.max_concurrent == 1, f"동시 {fm.max_concurrent} — 직렬이어야 한다"


def test_refine_accumulates_into_one_note() -> None:
    fm = FakeFM(reply="성인 1회 최대 1,000mg. 1일 4,000mg.")
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(60.0), strategy="refine")
    assert calls > 0
    assert "1,000mg" in packed[0][1], packed[0][1][:200]


def test_refine_reports_unread_chunks_when_time_runs_out() -> None:
    """읽다 만 것을 조용히 넘기면 모델은 문서를 다 읽은 요약이라고 믿는다."""

    class Slow(FakeFM):
        def __init__(self, dl):
            super().__init__(delay=0.01)
            self.dl = dl

        async def __call__(self, *a, **kw):
            # 한 청크 읽을 때마다 시간이 크게 흐른 것처럼 만든다.
            self.dl.started -= 20.0
            return await super().__call__(*a, **kw)

    dl = Deadline.start(60.0)
    fm = Slow(dl)
    packed, calls = run([Ev(text=BULK)], fm, deadline=dl, strategy="refine")
    assert calls >= 1
    assert "not read" in packed[0][1], packed[0][1][-200:]


def test_refine_failure_keeps_what_it_had() -> None:
    fm = FakeFM(raises=RuntimeError("상류가 죽었다"))
    packed, calls = run([Ev(text=BULK)], fm, deadline=Deadline.start(60.0), strategy="refine")
    assert packed and len(packed[0][1]) > 100, "근거가 사라졌다"


# ── 청크 분할 ─────────────────────────────────────────────────
def test_chunks_respect_paragraph_boundaries() -> None:
    """문장 중간에서 끊으면 숫자가 깨진다."""
    doc = "\n\n".join([f"문단 {i} 입니다. 용량은 {i}00mg 입니다." for i in range(60)])
    cs = D.chunks(doc, 300)
    assert len(cs) > 1
    for c in cs:
        assert not c.startswith(" ")
        assert "문단" in c
    # 모든 문단이 어딘가에는 들어가 있어야 한다.
    joined = "\n\n".join(cs)
    for i in (0, 30, 59):
        assert f"문단 {i} 입니다." in joined


def test_chunks_of_short_text_is_one() -> None:
    assert D.chunks("짧다", 6000) == ["짧다"]
    assert D.chunks("", 6000) == []


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
