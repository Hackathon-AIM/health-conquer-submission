"""출력 검증 단계 — 최종 답변을 내보내기 전에 한 번 더 본다.

generation 이 만든 초안을 그대로 흘리면, 모델이 무너진 턴에서 그대로 채점된다.
실측으로 본 무너지는 방식은 세 갈래였다.

  · 사고 토큰이 답변에 새어 나온다 (`<think>` 가 content 안에 섞인다)
  · 근거에 없는 번호를 인용한다 — 근거 2개인데 `[3]` 이 나온다
  · 질문 언어와 다른 언어로 답하거나, 같은 구절을 반복하며 길이만 늘린다

두 층으로 막는다.

  1. 규칙 검사 — 정규식으로 잡히는 것은 FM 을 부르지 않고 잡는다. 0초다.
  2. 리뷰 호출 — 규칙으로 못 잡는 것(위험한 용량 지시, 질문 회피, 근거와
     어긋나는 서술)은 모델에게 초안을 다시 보여 주고 판정을 받는다.

리뷰는 판정만 하지 않고 고친 답변까지 받아 온다. 판정만 받으면 문제를 알고도
고칠 호출이 한 번 더 필요한데, 시간 예산이 그걸 감당하지 못한다.

리뷰가 실패하거나 시간이 없으면 초안을 그대로 내보낸다 — 검증 때문에 답이
사라지는 것이 검증하지 않는 것보다 나쁘다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("verify")

# 답변 길이 상한. 이보다 길면 대개 반복으로 붕괴한 경우다.
MAX_CHARS = 3500

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
_HANGUL_RE = re.compile(r"[가-힣]")
_CITE_RE = re.compile(r"\[(\d{1,2})\]")
# 같은 문자가 20번 넘게 이어지면 정상 문장이 아니다 (`......`, `!!!!!!`).
_RUN_RE = re.compile(r"(.)\1{19,}")
_CODE_FENCE_RE = re.compile(r"```")
# 답을 주지 않고 진료 권유로만 끝나는 짧은 답. 회피는 정확성 축에서 손해다.
_DEFLECT_RE = re.compile(
    r"(전문가와 상담|의사와 상담|병원을 방문|consult (a|your) (doctor|professional))"
)

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "revise"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "final": {"type": "string"},
    },
    "required": ["verdict", "issues", "final"],
    "additionalProperties": False,
}

VERIFY_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "verification", "schema": VERIFY_SCHEMA},
}

VERIFY_PROMPT = """You are the last reviewer before this answer is sent to a user.

Check the DRAFT against every item below.

1. Leaked reasoning: no `<think>` tags, no "let me think", no meta commentary about
   how the answer was produced.
2. Language: the answer must be written in {lang_name}, the language of the QUESTION.
   No sentences in another language, no stray non-{lang_name} words.
3. Citations: the answer may cite only [1]..[{n_evidence}]. Any other bracket number is
   fabricated and must be removed together with the claim that leans on it.
   If EVIDENCE is empty the answer must cite nothing at all.
4. Evidence agreement: nothing may contradict a passage of EVIDENCE that actually bears
   on the QUESTION. Off-topic EVIDENCE is simply irrelevant — the answer may and should
   still answer from established medical knowledge without citing it. Never replace a
   correct answer with a statement that the evidence was insufficient or off-topic.
5. Safety: doses, intervals and contraindications must be stated carefully. Remove any
   instruction that could harm the reader if followed literally.
6. Text integrity: no code fences, no repeated runs of the same phrase or punctuation,
   no broken or garbled characters, no unfinished sentence at the end.
7. Substance: the answer must actually answer the QUESTION. Telling the reader to see a
   professional is fine as an addition, never as a replacement for the answer.
{defect_note}
Set "verdict" to "ok" only if every item passes. Then copy the DRAFT into "final"
without changing a character.

Otherwise set "verdict" to "revise", list what was wrong in "issues", and put the
corrected complete answer in "final". Keep the parts that were fine — rewrite, do not
shorten the answer into a summary.

QUESTION:
{question}

EVIDENCE ({n_evidence} items):
{evidence}

DRAFT:
{draft}"""


def strip_think(content: str) -> tuple[str, bool]:
    """새어 나온 사고 토큰을 걷어내고, 걷어냈는지도 돌려준다."""
    original = content
    if "</think>" in content.lower():
        # `<think>...</think>답변` 형태면 뒤쪽 답변만 남긴다.
        content = _THINK_TAG_RE.split(content)[-1]
    content = _THINK_BLOCK_RE.sub("", content)
    content = _THINK_TAG_RE.sub("", content).strip()
    return content, content != original.strip()


def local_defects(content: str, question: str, n_evidence: int) -> list[str]:
    """FM 을 부르지 않고 잡히는 결함. 리뷰 프롬프트에 그대로 붙여 준다."""
    defects: list[str] = []

    if _CITE_RE.search(content):
        cited = {int(m) for m in _CITE_RE.findall(content)}
        bad = sorted(n for n in cited if n < 1 or n > n_evidence)
        if bad:
            defects.append(
                f"cites {bad} but only [1]..[{n_evidence}] exist"
                if n_evidence
                else f"cites {bad} but there is no evidence to cite"
            )

    if _HANGUL_RE.search(question) and not _HANGUL_RE.search(content):
        defects.append("question is Korean but the answer contains no Korean")

    if _RUN_RE.search(content):
        defects.append("a character repeats 20+ times — text likely collapsed")

    if _CODE_FENCE_RE.search(content):
        defects.append("contains a code fence")

    if len(content) > MAX_CHARS:
        defects.append(f"{len(content)} characters — over the {MAX_CHARS} limit")

    if len(content) < 220 and _DEFLECT_RE.search(content):
        defects.append("short answer that only refers the reader elsewhere")

    return defects


def _extract_json(text: str) -> dict[str, Any] | None:
    """router 와 같은 이유로 관대하게 판다 — 스키마를 줘도 앞뒤에 말이 붙어 온다."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : start + (end - start + 1)])
    except json.JSONDecodeError:
        return None


def _truncate_at_sentence(content: str) -> str:
    """상한을 넘겼으면 문장 경계에서 끊는다. 문장 중간에서 자르면 더 나빠 보인다."""
    if len(content) <= MAX_CHARS:
        return content
    cut = max(content.rfind(". ", 0, MAX_CHARS), content.rfind("다. ", 0, MAX_CHARS))
    return content[: cut + 1 if cut > MAX_CHARS // 2 else MAX_CHARS].rstrip()


async def verify(
    content: str,
    question: str,
    evidence: str,
    n_evidence: int,
    call_fm,
    max_tokens: int,
    lang: str = "ko",
    deadline=None,
    reserve: float = 8.0,
) -> tuple[str, list[str]]:
    """초안을 검사하고, 필요하면 고쳐서 돌려준다. (최종 답변, 발견된 문제) 를 준다.

    `deadline` 에 `reserve` 초가 남지 않으면 리뷰 호출을 건너뛴다. 규칙 검사는
    공짜라 언제나 돈다.
    """
    content, leaked = strip_think(content)
    if not content:
        return content, ["draft was empty"]

    defects = local_defects(content, question, n_evidence)
    if leaked:
        defects.insert(0, "reasoning tokens leaked into the answer (removed locally)")

    if deadline is not None and deadline.expired(reserve=reserve):
        # 시간이 없으면 규칙으로 잡은 것만 처리하고 내보낸다.
        log.info("시간 예산으로 리뷰 호출 생략 (남은 %.0fs, 결함 %d개)", deadline.remaining(), len(defects))
        return _truncate_at_sentence(content), defects

    prompt = VERIFY_PROMPT.format(
        lang_name="Korean" if lang == "ko" else "English",
        n_evidence=n_evidence,
        evidence=evidence.strip() or "(none)",
        question=question[:1200],
        draft=content,
        defect_note=(
            "\nA mechanical pre-check already flagged: " + "; ".join(defects) + "\n"
            if defects
            else ""
        ),
    )

    try:
        data = await call_fm(
            [{"role": "user", "content": prompt}],
            min(max_tokens, 1600),
            {"response_format": VERIFY_RESPONSE_FORMAT},
        )
        parsed = _extract_json(data["choices"][0]["message"].get("content") or "")
    except Exception as e:  # noqa: BLE001 — 검증이 죽어도 답변은 나가야 한다
        log.warning("리뷰 호출 실패: %s: %s", type(e).__name__, str(e)[:300])
        parsed = None

    if not parsed:
        return _truncate_at_sentence(content), defects

    issues = [str(i) for i in (parsed.get("issues") or [])]
    final = str(parsed.get("final") or "").strip()
    final, _ = strip_think(final)

    if parsed.get("verdict") == "revise" and final:
        # 고친 답이 초안의 3분의 1도 안 되면 요약해 버린 것이다. 그건 손해다.
        if len(final) < len(content) // 3:
            log.warning("리뷰가 답을 %d→%d자로 줄여 초안을 유지한다", len(content), len(final))
            return _truncate_at_sentence(content), issues or defects
        log.info("리뷰 수정 적용 — %s", "; ".join(issues)[:200] or "사유 없음")
        return _truncate_at_sentence(final), issues

    return _truncate_at_sentence(final or content), issues
