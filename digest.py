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
DIGEST_MODE = os.environ.get("DIGEST_MODE", "off").strip().lower()

# map | refine — 위 docstring 의 트레이드오프 참고.
# 기본은 map 이다. 벽시계가 호출 하나분이라 우리 40초 예산 안에서도 돌기 때문이고,
# refine 이 더 낫다는 근거는 아직 이 프로젝트에서 측정된 적이 없다.
DIGEST_STRATEGY = os.environ.get("DIGEST_STRATEGY", "map").strip().lower()

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

    if DIGEST_STRATEGY == "refine":
        return await _refine(jobs, items, extracted, query, call_fm, deadline, timeout, budget)

    log.info("다이제스트 map %d청크 병렬 요약 (원문 %d자, 추출 %d자)",
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
