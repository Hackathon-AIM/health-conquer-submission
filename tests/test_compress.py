"""근거 압축 회귀 테스트 — 네트워크 없이 돈다.

    python tests/test_compress.py

지키려는 것
  1. 예산 안에 담긴다. (도구 하나가 20~30KB 를 돌려준다)
  2. **질문에 답하는 문단이 살아남는다.** 앞에서부터 자르면 답이 뒤에 있을 때
     통째로 잃는다 — 이게 단순 절단과의 차이 전부다.
  3. 원문 순서를 유지한다. 점수 순으로 이어붙이면 문맥이 깨진다.
  4. 중복 문단을 버린다. 같은 페이지가 도구 여러 개에서 겹쳐 온다.
  5. 버린 것이 있으면 그 사실을 밝힌다. 조용히 자르면 모델이 이게 전부라고 믿는다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import compress as C  # noqa: E402


@dataclass
class Ev:
    text: str
    relevance: float = 0.0
    title: str = ""
    url: str = ""
    source_type: str = ""


NOISE = (
    "이 문서는 요양급여 적정성 평가 사업의 배경과 추진 경과를 설명한다. "
    "평가 사업은 여러 해에 걸쳐 확대되어 왔으며 관련 위원회의 심의를 거쳐 운영된다. "
)
ANSWER = (
    "성인의 아세트아미노펜 1회 최대 용량은 1,000mg 이며 24시간 총량은 4,000mg 을 "
    "넘기지 않는다. 만성 음주자나 간질환 환자에서는 1일 2,000mg 이하로 낮춘다."
)
Q = "아세트아미노펜 성인 1회 최대 용량이 얼마인가요"


def test_answer_paragraph_survives_when_it_is_last() -> None:
    """앞에서부터 자르는 방식이 지는 자리. 답이 문서 끝에 있어도 남아야 한다."""
    doc = "\n\n".join([NOISE] * 12 + [ANSWER])
    out, dropped = C.prune_text(doc, Q, budget=600)
    assert "1,000mg" in out, out[:300]
    assert len(out) <= 700, len(out)
    assert dropped > 0
    # 단순 절단이었다면 답이 사라졌을 것이다.
    assert "1,000mg" not in doc[:600]


def test_keeps_original_order() -> None:
    first = "타이레놀은 아세트아미노펜 제제이다. 성인 용량 기준이 아래에 있다."
    second = ANSWER
    doc = "\n\n".join([first, NOISE, second])
    out, _ = C.prune_text(doc, Q, budget=400)
    assert out.index("제제이다") < out.index("1,000mg"), out


def test_numeric_paragraph_is_preferred_on_a_tie() -> None:
    """용량·기준치처럼 숫자가 든 문단이 답을 결정하는 경우가 많다."""
    plain = "아세트아미노펜 용량은 환자 상태에 따라 달라질 수 있다."
    numeric = "아세트아미노펜 용량은 1회 1,000mg, 1일 4,000mg 을 넘기지 않는다."
    doc = plain + "\n\n" + numeric
    out, _ = C.prune_text(doc, Q, budget=len(numeric) + 10)
    assert "1,000mg" in out, out


def test_short_text_is_untouched() -> None:
    out, dropped = C.prune_text(ANSWER, Q, budget=10_000)
    assert out == ANSWER and dropped == 0


def test_budget_is_respected_even_for_one_huge_paragraph() -> None:
    """문단 하나가 예산보다 커도 빈손으로 돌아가지 않는다."""
    out, _ = C.prune_text("가" * 5000, Q, budget=500)
    assert 0 < len(out) <= 500


# ── pack: 항목 여러 개 ────────────────────────────────────────
def test_pack_fits_the_budget() -> None:
    items = [Ev(text="\n\n".join([NOISE] * 20)) for _ in range(5)]
    packed = C.pack(items, Q, budget=4000)
    assert sum(len(t) for _, t in packed) <= 4000


def test_pack_gives_every_item_a_share() -> None:
    """한 항목이 20페이지라고 나머지를 굶기면 안 된다."""
    huge = Ev(text="\n\n".join([NOISE] * 40))
    small_a = Ev(text=ANSWER)
    small_b = Ev(text="이부프로펜 성인 1회 용량은 200~400mg 이다.")
    packed = C.pack([huge, small_a, small_b], Q, budget=3000)
    kept = " ".join(t for _, t in packed)
    assert "1,000mg" in kept, "작은 항목이 굶었다"
    assert len(packed) == 3, f"{len(packed)}개만 담겼다"


def test_pack_drops_duplicates() -> None:
    """같은 페이지가 도구 여러 개에서 겹쳐 온다 — 실측으로 봤다."""
    a = Ev(text=ANSWER)
    b = Ev(text=ANSWER)  # 같은 내용
    c = Ev(text="이부프로펜 성인 1회 용량은 200~400mg 이다.")
    packed = C.pack([a, b, c], Q, budget=4000)
    assert len(packed) == 2, [t[:30] for _, t in packed]


def test_pack_puts_the_best_first_and_second_best_last() -> None:
    """긴 맥락의 가운데는 덜 읽힌다 — 중요한 것을 양 끝에 둔다."""
    best = Ev(text="가장 관련 있는 근거 " + ANSWER, relevance=0.9)
    mid = Ev(text="중간 근거 " + NOISE, relevance=0.3)
    second = Ev(text="두 번째 근거 이부프로펜 200mg", relevance=0.7)
    packed = C.pack([mid, best, second], Q, budget=4000)
    assert packed[0][0] is best, "1등이 맨 앞이 아니다"
    assert packed[-1][0] is second, "2등이 맨 뒤가 아니다"


def test_pack_handles_empty() -> None:
    assert C.pack([], Q, budget=4000) == []


# ── as_prompt 통합 ───────────────────────────────────────────
def test_as_prompt_respects_budget_and_reports_drops() -> None:
    from retrieval import Evidence, RetrievalResult

    res = RetrievalResult(status="sufficient", query=Q)
    res.items = [
        Evidence(cite_uid=f"c{i}", title=f"t{i}", text="\n\n".join([NOISE] * 30))
        for i in range(6)
    ]
    out = res.as_prompt(budget=3000)
    assert len(out) <= 3600, len(out)
    assert "[1]" in out


def test_as_prompt_keeps_the_answer_bearing_item() -> None:
    from retrieval import Evidence, RetrievalResult

    res = RetrievalResult(status="sufficient", query=Q)
    res.items = [
        Evidence(cite_uid="noise", title="배경", text="\n\n".join([NOISE] * 30)),
        Evidence(cite_uid="ans", title="용량", text="\n\n".join([NOISE] * 10 + [ANSWER])),
    ]
    out = res.as_prompt(budget=2000)
    assert "1,000mg" in out, out[-400:]


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
