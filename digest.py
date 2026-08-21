"""근거 다이제스트 — 추출로 안 줄어드는 크기가 왔을 때만 요약한다.

이 파일이 있는 이유
  compress.py 의 추출식 가지치기는 LLM 호출 0회로 문단을 고른다. 대부분은 그걸로
  충분하다 — 실측에서 26,546자가 2,930자로 줄었다. 하지만 원문이 **질의어와 고르게
  겹치는 큰 덩어리**일 때는 문단 선택이 잘 안 듣는다. 어느 문단이나 비슷하게
  관련 있어 보이면 앞에서부터 예산까지 담다 마는 것과 다를 게 없어진다.
  그때는 압축을 하려면 읽고 줄이는 수밖에 없고, 그건 모델이 해야 한다.

두 가지 전략을 둔다 — map(병렬) 과 refine(순차)
  refine 은 앞에서부터 하나씩 읽으며 누적 요약을 갱신한다. 청크 N개면 LLM 호출
  N번이 **직렬**로 붙는다. 실측으로 계산하면:
      가장 큰 MCP 결과 29.4KB → 6,000자 청크 5개
      L2 호출 1회(thinking off) 중앙 5.3s · 최대 17.1s
      순차 5회 ≈ 27s ~ 85s

  이 시간이 감당되는지는 **누구의 시계로 보느냐**에 달렸다.
      우리 자체 예산 REQUEST_BUDGET_S = 40s  → 안 된다
      평가자 client timeout                  → conquer_val/test 180s, 기본 360s
  즉 평가자 기준으로는 여유가 있다. 우리 40초는 우리가 정한 값이다(예산 75초
  회차가 0.00 을 받은 뒤 줄인 값인데, 그 0.00 의 원인이 지연이라는 것은 팀의
  추정이지 분리된 관측이 아니다). 그래서 refine 을 막지 않고 **고를 수 있게** 둔다.

  map: 청크를 동시에 요약한다 → 벽시계는 호출 1회분. reduce 호출은 없다.
       요약본들이 이미 작아서 이어붙이면 그만이다.
  refine: 앞 요약을 보면서 다음 청크를 읽는다. 문서 전체를 관통하는 맥락
       (앞에서 정의한 용어가 뒤에서 쓰이는 표·기준표)이 있을 때 map 보다 낫다.
       대신 청크 수만큼 지연이 쌓이고, 앞부분이 반복 요약돼 정보가 마모된다.

  둘 다 시간이 떨어지면 **읽다 만 지점에서 멈추고, 몇 청크를 못 읽었는지 밝힌다.**
  조용히 멈추면 모델은 문서를 다 읽은 요약이라고 믿는다.

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
#
# 기본은 off 다. 한때 auto 로 뒀었는데, 그 판단의 근거였던 "추출이 답을 버린다"
# 가 예산 4,000자에서만 참이었다. 예산을 12,000자로 올리고 다시 재보니:
#
#   원문 27,031자 (예산의 2.3배)
#     추출만    12,233자 · 호출 0회 · 0.0초 · 놓친 신호 1개
#     전량요약   5,382자 · 호출 9회 · 9.9초 · 놓친 신호 1개
#   원문 58,862자 (예산의 4.9배)
#     추출만    12,707자 · 호출 0회 · 0.0초 · 놓친 신호 없음
#     전량요약  23,200자 · 호출 16회 · 8.4초 · 놓친 신호 없음
#
# 추출이 원문을 그대로 남기면서 공짜다. 요약은 더 조밀한 표현을 만들지만
# 이 문서들에서는 그게 점수로 이어진다는 증거가 없다. 켜는 것은 A/B 이후다.
DIGEST_MODE = os.environ.get("DIGEST_MODE", "off").strip().lower()

# map | refine — map 은 청크를 동시에, refine 은 앞 요약을 보며 순차로.
DIGEST_STRATEGY = os.environ.get("DIGEST_STRATEGY", "map").strip().lower()

# 요약을 돌릴 조건은 절대 크기가 아니라 **예산 대비 크기**다.
#
# 실측이 이걸 가르쳤다. 같은 27,031자 문서·같은 질의로:
#   예산  4,000자 · 추출만 → 답이 전멸 (PSA·Gleason·10 ng/mL 전부 0)
#   예산 12,000자 · 추출만 → 그 신호가 전부 살아남음 · 호출 0회 · 0.0초
#   예산 12,000자 · 전량요약 → 5,382자로 더 조밀하지만 호출 9회 · 9.9초
# 즉 예산이 충분하면 추출이 이긴다. 원문 그대로이고 공짜다.
# 요약이 값을 내는 구간은 **원문이 예산보다 훨씬 클 때** 뿐이다.
DIGEST_TRIGGER_MULT = float(os.environ.get("DIGEST_TRIGGER_MULT", "2.0"))
# 그래도 이 크기 밑이면 굳이 부르지 않는다.
DIGEST_MIN_CHARS = int(os.environ.get("DIGEST_MIN_CHARS", "12000"))
# 남은 시간이 이보다 적으면 시작하지 않는다.
DIGEST_MIN_S = float(os.environ.get("DIGEST_MIN_S", "10"))

# ── 커버리지 ─────────────────────────────────────────────────
#
# 목표는 **원문 전체를 덮는 것**이다. 예전 상한 4청크는 27,031자 문서의 80% 만
# 덮었고, 못 덮은 조각은 추출식으로 떨어져 대부분 버려졌다.
#
# 청크를 작게 잡을수록 요약이 촘촘해지지만 호출이 늘어난다. 병렬이라 벽시계는
# 호출 수가 아니라 **파도 수**(청크수 ÷ 동시성)에 비례한다.
#   27,031자 · 청크 3,500자 → 8청크 · 동시 8 → 한 파도 ≈ 5~6초
DIGEST_CHUNK_CHARS = int(os.environ.get("DIGEST_CHUNK_CHARS", "3500"))
DIGEST_CONCURRENCY = int(os.environ.get("DIGEST_CONCURRENCY", "8"))
# 병리적인 경우를 막는 안전 상한. 여기 걸리면 못 덮은 조각은 추출식으로 가고,
# 그 사실을 근거 블록에 적는다 — 조용히 빠지면 모델이 전부라고 믿는다.
DIGEST_MAX_CALLS = int(os.environ.get("DIGEST_MAX_CALLS", "16"))

# map 호출 하나에 걸 상한.
DIGEST_CALL_CAP_S = float(os.environ.get("DIGEST_CALL_CAP_S", "12"))
# 청크 하나당 뽑아낼 출력 토큰. 원문 3,500자에서 사실만 남기면 이 정도면 충분하다.
DIGEST_OUT_TOKENS = int(os.environ.get("DIGEST_OUT_TOKENS", "500"))

MAP_PROMPT = """Extract from the SOURCE only what could help answer the QUESTION.

Rules:
- Copy numbers, doses, ages, thresholds, code numbers, article numbers, dates and
  drug names EXACTLY as they appear. Never round, never paraphrase a number.
- Keep the condition attached to every fact. "1,000mg" and "1,000mg in adults without
  liver disease" are different facts; dropping the condition makes the note wrong.
- Keep negations and exclusions. "Not indicated for", "contraindicated in", "except"
  carry as much weight as the positive statements.
- Write short factual lines, not prose. No preamble, no conclusion.
- If the SOURCE contains nothing relevant to the QUESTION, reply with exactly: NONE
- Do not add anything that is not in the SOURCE, and do not resolve a contradiction
  inside the source — record both sides.

QUESTION:
{query}

SOURCE:
{chunk}"""


REFINE_PROMPT = """You are building running notes to answer the QUESTION.

You already have NOTES SO FAR. Read the NEW SOURCE and return the updated notes.

Rules:
- Keep every fact already in the notes. Drop one only if the new source explicitly
  corrects it, and then say so on that line.
- Add only what is new and could help answer the question.
- Copy numbers, doses, ages, thresholds, code numbers, article numbers, dates and
  drug names EXACTLY as they appear. Never round, never paraphrase a number.
- Keep the condition attached to every fact, and keep negations and exclusions.
- Short factual lines, not prose. No preamble.
- Return the COMPLETE updated notes, not a diff. Notes you leave out are lost —
  nothing downstream can recover them.

QUESTION:
{query}

NOTES SO FAR:
{notes}

NEW SOURCE:
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
    if raw_chars < budget * DIGEST_TRIGGER_MULT:
        # 예산으로 감당되는 크기다. 추출이 원문을 그대로 남기고 공짜다.
        return f"예산으로 감당된다 (원문 {raw_chars}자 vs 예산 {budget}자)"
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
    """가져온 원문 **전체**를 조각내어 요약하고, 그 요약으로 근거를 만든다.

    추출식 선택(compress.pack)은 예산 밖의 문단을 그냥 버린다. 실측에서 원문의
    6~15% 만 남았고, 답을 결정하는 수치가 통째로 사라지는 것을 봤다.
    요약은 버리지 않고 줄인다 — 그게 이 함수가 있는 이유다.

    돌려주는 것: (담긴 (항목, 텍스트) 목록, 쓴 호출 수)
    실패하거나 시간이 없으면 그 조각만 추출식으로 대신한다. 전부 실패하면
    추출 결과를 그대로 돌려준다 — 요약 때문에 근거가 사라지는 일은 없다.
    """
    items = list(items)
    extracted = pack(items, query, budget=budget, per_item_cap=per_item_cap)

    raw_chars = sum(len(getattr(it, "text", "") or "") for it in items)
    extracted_chars = sum(len(t) for _, t in extracted)
    skip = _should_run(raw_chars, extracted_chars, budget, deadline)
    if skip:
        log.debug("다이제스트 생략 — %s", skip)
        return extracted, 0

    # ── 모든 항목의 모든 조각을 일감으로 만든다 ──────────────
    # 예전에는 큰 항목부터 상한까지만 담고 나머지를 버렸다. 그래서 문서의
    # 80% 만 덮였고, 못 덮은 20% 는 추출식으로 떨어져 대부분 사라졌다.
    jobs: list[tuple[object, str]] = []
    for it in items:
        for ch in chunks(getattr(it, "text", "") or "", DIGEST_CHUNK_CHARS):
            jobs.append((it, ch))
    if not jobs:
        return extracted, 0

    total_jobs = len(jobs)
    uncovered = 0
    if total_jobs > DIGEST_MAX_CALLS:
        # 안전 상한. 못 덮은 조각은 버리지 않고 추출식으로 보낸다.
        uncovered = total_jobs - DIGEST_MAX_CALLS
        jobs = jobs[:DIGEST_MAX_CALLS]
        log.info("다이제스트 상한 — %d조각 중 %d조각만 요약한다", total_jobs, len(jobs))

    timeout = call_cap(deadline, DIGEST_CALL_CAP_S, reserve=0.0)

    if DIGEST_STRATEGY == "refine":
        return await _refine(jobs, items, extracted, query, call_fm, deadline, timeout, budget)

    sem = asyncio.Semaphore(max(1, DIGEST_CONCURRENCY))

    async def one(chunk: str) -> str:
        async with sem:
            if deadline is not None and deadline.expired(reserve=0.0):
                raise TimeoutError("digest 시간 소진")
            data = await call_fm(
                [{"role": "user", "content": MAP_PROMPT.format(query=query[:600], chunk=chunk)}],
                DIGEST_OUT_TOKENS,
                {"chat_template_kwargs": {"enable_thinking": False}},
                timeout=timeout,
            )
            return (data["choices"][0]["message"].get("content") or "").strip()

    log.info(
        "다이제스트 map — 원문 %d자 · %d조각 · 동시 %d (추출만 하면 %d자였다)",
        raw_chars, len(jobs), DIGEST_CONCURRENCY, extracted_chars,
    )
    results = await asyncio.gather(*(one(ch) for _, ch in jobs), return_exceptions=True)

    # 항목별로 요약을 모은다. 실패한 조각은 그 항목의 추출본으로 메운다.
    by_item: dict[int, list[str]] = {}
    used_calls = 0
    failed = 0
    for (it, _), r in zip(jobs, results):
        if isinstance(r, BaseException):
            failed += 1
            continue
        used_calls += 1
        if not r or r.strip().upper() == "NONE":
            continue
        by_item.setdefault(id(it), []).append(r.strip())

    if failed:
        log.warning("조각 요약 %d/%d 실패 — 그만큼은 추출본으로 메운다", failed, len(jobs))
    if not by_item:
        log.info("다이제스트가 아무것도 못 건졌다 — 추출 결과를 쓴다")
        return extracted, used_calls

    digested: list[tuple[object, str]] = []
    for it in items:
        summ = by_item.get(id(it))
        fallback = next((t for o, t in extracted if o is it), "")
        if summ:
            text = "\n".join(summ)
            if failed or uncovered:
                # 못 덮은 부분이 있으면 추출본을 덧붙여 그 자리를 메운다.
                if fallback and fallback not in text:
                    text = text + "\n\n[from the un-condensed source]\n" + fallback
        else:
            text = fallback
        if not text:
            continue
        digested.append((it, text))

    if uncovered:
        log.info("요약하지 못한 %d조각은 추출본으로 대신했다", uncovered)

    # 요약본도 예산을 지켜야 한다. 조각마다 500토큰씩 받으면 16조각이 2만 자를
    # 넘길 수 있다 — 실측에서 58,862자 원문에 23,200자를 만들어 예산 12,000자를
    # 넘겼다. 넘치면 요약본 자체를 다시 한 번 조인다.
    total = sum(len(t) for _, t in digested)
    if total > budget:
        share = max(300, budget // max(1, len(digested)))
        digested = [(it, prune_text(t, query, share)[0]) for it, t in digested]
        total = sum(len(t) for _, t in digested)
    log.info("다이제스트 완료 — 호출 %d회 · %d자 (원문 %d자의 %.0f%%)",
             used_calls, total, raw_chars, total / max(1, raw_chars) * 100)
    return digested, used_calls


async def _refine(jobs, items, extracted, query, call_fm, deadline, timeout, budget):
    """앞에서부터 하나씩 읽으며 누적 요약을 갱신한다. (담긴 목록, 쓴 호출 수)

    map 과 달리 직렬이라 청크 수만큼 지연이 쌓인다. 그 대신 앞에서 정의된 것을
    뒤에서 쓰는 문서(용어 정의 → 기준표 같은)에서 맥락이 이어진다.

    시간이 떨어지면 읽다 만 지점에서 멈추고 **몇 청크를 못 읽었는지 남긴다.**
    조용히 멈추면 모델은 문서를 다 읽은 요약이라고 믿는다.
    """
    notes = ""
    used_calls = 0
    read = 0
    for it, chunk in jobs:
        if deadline is not None and deadline.remaining() < DIGEST_MIN_S:
            log.info("refine 중단 — 남은 %.1fs (%d/%d 청크만 읽었다)",
                     deadline.remaining(), read, len(jobs))
            break
        try:
            data = await call_fm(
                [{"role": "user", "content": REFINE_PROMPT.format(
                    query=query[:600], notes=notes or "(none yet)", chunk=chunk)}],
                DIGEST_OUT_TOKENS,
                {"chat_template_kwargs": {"enable_thinking": False}},
                timeout=timeout,
            )
            used_calls += 1
            out = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:  # noqa: BLE001 — 한 청크가 죽어도 앞의 노트는 살린다
            log.warning("refine 청크 실패: %s — 지금까지의 노트를 유지한다", type(e).__name__)
            break
        if out and out.strip().upper() != "NONE":
            notes = out
        read += 1

    if not notes:
        log.info("refine 이 아무것도 못 건졌다 — 추출 결과를 쓴다")
        return extracted, used_calls

    unread = len(jobs) - read
    if unread > 0:
        notes += f"\n[... {unread} more chunks of the source were not read (time budget) ...]"

    # 누적 노트는 원본 항목 중 가장 큰 것에 붙인다 — 그 항목을 대표로 인용하게 된다.
    anchor = max(items, key=lambda x: len(getattr(x, "text", "") or ""))
    packed = [(anchor, notes)]
    for it in items:
        if it is anchor:
            continue
        text = next((t for o, t in extracted if o is it), "")
        if text:
            packed.append((it, text))

    total = sum(len(t) for _, t in packed)
    if total > budget:
        share = max(200, budget // max(1, len(packed)))
        packed = [(it, prune_text(t, query, share)[0]) for it, t in packed]
    log.info("refine 완료 — 호출 %d회 · %d/%d 청크 · %d자",
             used_calls, read, len(jobs), sum(len(t) for _, t in packed))
    return packed, used_calls
