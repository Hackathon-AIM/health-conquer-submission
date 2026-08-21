"""근거 다이제스트 — 추출로 안 줄어드는 크기가 왔을 때만 요약한다.

이 파일이 있는 이유
  compress.py 의 추출식 가지치기는 LLM 호출 0회로 문단을 고른다. 대부분은 그걸로
  충분하다 — 실측에서 26,546자가 2,930자로 줄었다. 하지만 원문이 **질의어와 고르게
  겹치는 큰 덩어리**일 때는 문단 선택이 잘 안 듣는다. 어느 문단이나 비슷하게
  관련 있어 보이면 앞에서부터 예산까지 담다 마는 것과 다를 게 없어진다.
  그때는 압축을 하려면 읽고 줄이는 수밖에 없고, 그건 모델이 해야 한다.

왜 "앞에서부터 요약→요약→요약"(refine chain)이 아닌가
  순차 refine 은 청크 N개면 LLM 호출 N번이 **직렬**로 붙는다. 실측으로 계산하면:
      가장 큰 MCP 결과 29.4KB → 6,000자 청크 5개
      L2 호출 1회(thinking off) 중앙 5.3s · 최대 17.1s
      순차 5회 ≈ 27s ~ 85s
  검색 단계에 남는 시간은 40s − 26s(답변·검증 몫) = 14s 다. 요약만으로 예산을
  2~6배 넘긴다. 이 저장소에는 그 실패의 기록이 이미 있다 — 예산 75초로 돌린
  회차가 0.00 을 받았고, 원인은 답이 나빠서가 아니라 도달하지 못해서였다.

  그래서 **map 만 병렬로 한 라운드** 돌린다.
      · 청크들을 동시에 요약한다 → 벽시계는 호출 1회분이다
      · reduce 호출은 두지 않는다. 요약본들은 이미 작아서 이어붙이면 그만이다
      · 실패·시간초과한 청크는 추출식 결과로 대신한다 (부분 실패가 전체를 죽이지 않는다)

  refine 이 더 매끄러운 요약을 만드는 것은 맞다. 앞 요약을 보면서 뒤를 쓰니까.
  하지만 여기서 필요한 것은 매끄러운 산문이 아니라 **사실 목록**이고, 그건 청크마다
  독립으로 뽑아도 손해가 거의 없다.

기본은 꺼짐이다
  요청당 상류 호출 수가 곧 지연이고, 이 저장소에서 지연은 점수와 직결됐다.
  DIGEST_MODE=auto 로 켜고 A/B 로 재고 나서 채택할 것.
"""

from __future__ import annotations

import asyncio
import logging
import os

from budget import call_cap
from compress import pack, prune_text

log = logging.getLogger("digest")

# off | auto
DIGEST_MODE = os.environ.get("DIGEST_MODE", "off").strip().lower()

# 원문 총량이 이보다 작으면 요약할 이유가 없다. 추출로 충분하다.
#
# 처음에는 "추출 결과가 예산 대비 몇 배인가" 로도 막으려 했는데 그건 성립하지
# 않는다 — pack() 은 언제나 예산 이하를 돌려주므로 그 비율은 항상 1 이하다.
# 추출이 잘 됐는지는 읽어보지 않고는 알 수 없다. 그래서 크기·시간·호출수만 본다.
DIGEST_MIN_CHARS = int(os.environ.get("DIGEST_MIN_CHARS", "20000"))
# 남은 시간이 이보다 적으면 시작하지 않는다.
DIGEST_MIN_S = float(os.environ.get("DIGEST_MIN_S", "12"))
# map 호출 하나에 걸 상한과 개수 상한. 개수를 막지 않으면 100페이지짜리가 오면
# 호출이 십수 개로 늘고, 그 순간 상류가 우리 때문에 밀린다.
DIGEST_CALL_CAP_S = float(os.environ.get("DIGEST_CALL_CAP_S", "10"))
DIGEST_MAX_CALLS = int(os.environ.get("DIGEST_MAX_CALLS", "4"))
DIGEST_CHUNK_CHARS = int(os.environ.get("DIGEST_CHUNK_CHARS", "6000"))
DIGEST_OUT_TOKENS = int(os.environ.get("DIGEST_OUT_TOKENS", "400"))

MAP_PROMPT = """Extract from the SOURCE only what could help answer the QUESTION.

Rules:
- Copy numbers, doses, ages, thresholds, code numbers, article numbers, dates and
  drug names EXACTLY as they appear. Never round, never paraphrase a number.
- Write short factual lines, not prose. No preamble, no conclusion.
- If the SOURCE contains nothing relevant to the QUESTION, reply with exactly: NONE
- Do not add anything that is not in the SOURCE.

QUESTION:
{query}

SOURCE:
{chunk}"""


def chunks(text: str, size: int) -> list[str]:
    """문단 경계를 지키며 size 안팎으로 자른다. 문장 중간에서 끊으면 숫자가 깨진다."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    used = 0
    for para in text.split("\n\n"):
        p = para.strip()
        if not p:
            continue
        if used and used + len(p) > size:
            out.append("\n\n".join(buf))
            buf, used = [], 0
        # 문단 하나가 size 보다 크면 그것만 통째로 한 청크로 둔다.
        buf.append(p)
        used += len(p) + 2
    if buf:
        out.append("\n\n".join(buf))
    return out


def _should_run(raw_chars: int, extracted_chars: int, budget: int, deadline) -> str | None:
    """돌릴 이유가 없으면 그 이유를 돌려준다(None 이면 돌린다)."""
    if DIGEST_MODE != "auto":
        return "digest off"
    if raw_chars < DIGEST_MIN_CHARS:
        return f"원문이 작다 ({raw_chars}자 < {DIGEST_MIN_CHARS})"
    if extracted_chars >= raw_chars:
        return "추출이 아무것도 버리지 않았다"
    if deadline is not None and deadline.remaining() < DIGEST_MIN_S:
        return f"시간이 없다 (남은 {deadline.remaining():.0f}s)"
    return None


async def digest(
    items,
    query: str,
    budget: int,
    call_fm,
    deadline=None,
    per_item_cap: int = 1400,
) -> tuple[list, int]:
    """근거 항목들을 예산 안에 담는다. (담긴 (항목, 텍스트) 목록, 쓴 호출 수)

    먼저 추출로 줄여 보고, 그걸로 안 되는 크기일 때만 map 요약을 한 라운드 돌린다.
    어떤 실패에서도 추출 결과로 물러난다 — 요약 때문에 근거가 사라지면 안 된다.
    """
    items = list(items)
    extracted = pack(items, query, budget=budget, per_item_cap=per_item_cap)

    raw_chars = sum(len(getattr(it, "text", "") or "") for it in items)
    extracted_chars = sum(len(t) for _, t in extracted)
    skip = _should_run(raw_chars, extracted_chars, budget, deadline)
    if skip:
        log.debug("다이제스트 생략 — %s", skip)
        return extracted, 0

    # 큰 항목부터 요약 대상으로 삼는다. 작은 것은 추출본이 이미 원문에 가깝다.
    targets = sorted(items, key=lambda it: -len(getattr(it, "text", "") or ""))
    jobs: list[tuple[object, str]] = []
    for it in targets:
        for ch in chunks(getattr(it, "text", "") or "", DIGEST_CHUNK_CHARS):
            jobs.append((it, ch))
            if len(jobs) >= DIGEST_MAX_CALLS:
                break
        if len(jobs) >= DIGEST_MAX_CALLS:
            break
    if not jobs:
        return extracted, 0

    timeout = call_cap(deadline, DIGEST_CALL_CAP_S, reserve=0.0)

    async def one(chunk: str) -> str:
        data = await call_fm(
            [{"role": "user", "content": MAP_PROMPT.format(query=query[:600], chunk=chunk)}],
            DIGEST_OUT_TOKENS,
            {"chat_template_kwargs": {"enable_thinking": False}},
            timeout=timeout,
        )
        return (data["choices"][0]["message"].get("content") or "").strip()

    log.info("다이제스트 %d청크 병렬 요약 (원문 %d자, 추출 %d자)",
             len(jobs), raw_chars, extracted_chars)
    results = await asyncio.gather(*(one(ch) for _, ch in jobs), return_exceptions=True)

    # 항목별로 요약을 모은다. 실패한 청크는 그냥 빠진다.
    by_item: dict[int, list[str]] = {}
    used_calls = 0
    for (it, _), r in zip(jobs, results):
        if isinstance(r, BaseException):
            log.warning("청크 요약 실패: %s", type(r).__name__)
            continue
        used_calls += 1
        if not r or r.strip().upper() == "NONE":
            continue
        by_item.setdefault(id(it), []).append(r.strip())

    if not by_item:
        log.info("다이제스트가 아무것도 못 건졌다 — 추출 결과를 쓴다")
        return extracted, used_calls

    # 요약본으로 만든 대체 항목. 원본 메타데이터(title/url/cite_uid)는 그대로 쓴다.
    digested: list[tuple[object, str]] = []
    for it in items:
        summ = by_item.get(id(it))
        if summ:
            text = "\n".join(summ)
        else:
            # 요약 대상이 아니었던 항목은 추출본을 쓴다.
            text = next((t for o, t in extracted if o is it), "")
        if not text:
            continue
        digested.append((it, text))

    # 요약본이 예산을 넘을 수도 있다. 마지막으로 한 번 더 조인다.
    total = sum(len(t) for _, t in digested)
    if total > budget:
        share = max(200, budget // max(1, len(digested)))
        digested = [
            (it, prune_text(t, query, share)[0]) for it, t in digested
        ]
    log.info("다이제스트 완료 — 호출 %d회 · %d자", used_calls,
             sum(len(t) for _, t in digested))
    return digested, used_calls
