"""출력 검증층 — 내보내기 전에 "질문에 답했는가" 와 "이상한 데가 없는가" 를 본다.

왜 초안을 그대로 흘리면 안 되나
  L2 는 대체로 잘 쓰지만, 무너질 때는 조용히 무너진다. 실측으로 본 방식은 넷이다.
    · 사고 토큰이 답변에 샌다 (`<think>` 가 content 안에 섞인다)
    · max_tokens 에 걸려 문장 중간에서 끊긴다
    · 질문이 물은 것 중 일부를 통째로 빠뜨린다 — 특히 질문이 두 개 이상일 때
    · 답을 주지 않고 "전문가와 상담하세요" 로 끝낸다
  채점은 감점을 분모에서 빼기 때문에, 감점을 밟는 것이 못 쓴 것보다 비싸다.

왜 항상 L2 를 부르지 않는가
  이 저장소의 실측 이력이 일관되게 말한다 — 레이어를 얹을수록 점수가 내려갔고,
  기준선(raw L2)이 이겼다. 원인은 품질이 아니라 지연이었다. 요청당 FM 호출을
  1회에서 2회로 늘리면 그 자체가 위험이다.

  그래서 이 층은 **비용을 의심에 비례**시킨다.
    1단계 규칙 검사 — 0 호출, 0초. 정규식으로 잡히는 붕괴를 먼저 본다.
    2단계 적합성 신호 — 0 호출. 질문의 핵심어가 답변에 없거나, 질문이 여러 개인데
           답이 짧거나, 문장이 끊겼는지를 본다. **판정이 아니라 의심**이다.
    3단계 L2 리뷰 — 위에서 무언가 걸렸을 때만 부른다. 판정과 **고친 답**을 함께 받는다.
                    판정만 받으면 고칠 호출이 한 번 더 필요한데 예산이 감당 못 한다.

  `REVIEW_MODE=always` 로 3단계를 항상 켤 수 있다(A/B 용). `off` 면 이 층 전체가 없다.

무엇을 절대 하지 않는가
  검증 때문에 답이 사라지는 일은 없어야 한다. 리뷰가 실패하든 시간이 없든
  형식이 깨지든, 언제나 손에 있는 초안으로 물러난다. 빈 답은 확정 0점이다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from budget import answer_timeout

log = logging.getLogger("review")

# off | suspect | always
REVIEW_MODE = os.environ.get("REVIEW_MODE", "suspect").strip().lower()

# 커버리지 신호를 **리뷰 트리거로 쓸 것인가.**
#
# 기본은 끔이다. 실측 12문항에서 4건이 걸렸는데 전부 오탐이었다 — 원인은 동의어다.
# "먹어도 되나요" 에 "복용" 으로 답하고 "당뇨" 에 "혈당" 으로 답하면 토큰이 겹치지
# 않는다. 형태소 분석기 없이 겹침만 보는 방식의 한계이고, 오탐 하나가 곧 FM 호출
# 하나이며 그게 곧 지연이다.
#
# 신호 자체는 계속 계산해 로그에 남긴다 — 끄는 것은 "부를지" 뿐이다. 1 로 켜서 A/B.
REVIEW_COVERAGE = os.environ.get("REVIEW_COVERAGE", "0") == "1"

# 리뷰 호출 하나에 걸 상한과, 남은 시간이 없어도 보장하는 바닥.
#
# 답변 호출과 같은 논리다. 초안이 30초를 쓰고 나면 40초 예산에는 10초밖에 안 남는데,
# 그 10초로 4096토큰 수정본을 받을 수는 없다 — 실측에서 리뷰 호출이 전부
# ReadTimeout 으로 죽었다. 리뷰는 규칙 결함이 실제로 잡혔을 때만 돌고 그건 드문
# 일이므로(오탐 고친 뒤 실측 0/12), 그때는 시간을 주는 편이 맞다.
REVIEW_CAP_S = float(os.environ.get("REVIEW_CAP_S", "25"))
REVIEW_FLOOR_S = float(os.environ.get("REVIEW_FLOOR_S", "20"))
# 예산이 이만큼도 안 남았으면 리뷰를 시작하지 않는다. 규칙 검사는 공짜라 언제나 돈다.
REVIEW_RESERVE_S = float(os.environ.get("REVIEW_RESERVE_S", "2"))
# 리뷰 출력 토큰. 고친 답 전문을 받아야 해서 초안보다 넉넉해야 한다.
REVIEW_MAX_TOKENS = int(os.environ.get("REVIEW_MAX_TOKENS", "4096"))

# 답변 길이 상한. 이보다 길면 대개 반복으로 붕괴한 경우다.
MAX_CHARS = int(os.environ.get("REVIEW_MAX_CHARS", "6000"))

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
_HANGUL_RE = re.compile(r"[가-힣]")
_CITE_RE = re.compile(r"\[(\d{1,2})\]")
# 같은 문자가 20번 넘게 이어지면 정상 문장이 아니다 (`......`, `!!!!!!`).
_RUN_RE = re.compile(r"([^-=*_~#\s])\1{19,}")
# 마크다운 구분선(`-----`, `=====`)만으로 된 줄은 반복 붕괴가 아니다.
_MD_RULE_RE = re.compile(r"^[\s>*_=~#-]+$")
# 같은 줄이 그대로 반복되는 붕괴.
_DUP_LINE_MIN = 3
# 답을 주지 않고 진료 권유로만 끝나는 짧은 답. 회피는 정확성 축에서 손해다.
_DEFLECT_RE = re.compile(
    r"(전문가와 상담|의사와 상담|병원을 방문|진료를 받으|consult (a|your) (doctor|professional))"
)
# 문장이 제대로 끝났는가. 한국어 종결·구두점·목록 기호로 끝나면 완결로 본다.
#
# ★ 판정 전에 꼬리의 마크다운을 벗겨야 한다. 실측에서 "...바랍니다.**" 와
#   "...없습니다.*" 를 잘림으로 오인했다 — 굵게 표시를 닫은 별표였을 뿐이다.
#   오탐 하나가 멀쩡한 답변에 리뷰 호출을 붙이고, 그 호출이 곧 지연이다.
_CLOSED_RE = re.compile(r"[.!?。\)\]]$|[다요음임함]$|[:：]$")
_MD_TAIL_RE = re.compile("[\s*_`~\"'“”‘’]+$")


def looks_closed(body: str) -> bool:
    """꼬리의 굵게·인용부호를 벗기고 나서 완결 여부를 본다."""
    return bool(_CLOSED_RE.search(_MD_TAIL_RE.sub("", body.rstrip())))

# 조사·어미를 떼기 위한 꼬리들. 형태소 분석기를 넣을 수 없으니(의존성 최소 원칙)
# 접미 제거로 근사한다. 여기서 하는 일은 판정이 아니라 **의심 신호**라 이 정도로 족하다.
_PARTICLES = (
    "으로부터", "에서는", "에게서", "이라는", "라는", "에서", "에게", "으로", "까지", "부터",
    "이나", "나요", "까요", "인가", "는지", "은지", "이랑", "하고", "와의", "과의",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "로", "와", "과", "요",
)
_STOPWORDS = {
    "그리고", "그런데", "하지만", "무엇", "어떻게", "언제", "어디", "누구", "얼마", "정도",
    "경우", "때문", "위해", "대해", "관련", "가능", "필요", "생각", "이런", "저런", "그런",
    "있나", "있을", "하나", "지금", "오늘", "제가", "저는", "제일", "너무", "조금", "많이",
}
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}|\d+(?:\.\d+)?")


# ── 1단계: 규칙 검사 (0 호출) ─────────────────────────────────
def strip_think(content: str) -> tuple[str, bool]:
    """사고 블록을 걷어낸다. 닫는 태그가 없는 경우도 처리한다."""
    before = content
    content = _THINK_BLOCK_RE.sub("", content)
    if "<think>" in content.lower():
        # 열기만 하고 안 닫은 경우 — 그 뒤는 통째로 사고 토큰이다.
        content = re.split(r"<think>", content, flags=re.IGNORECASE)[0]
    content = _THINK_TAG_RE.sub("", content)
    return content.strip(), content.strip() != before.strip()


def _truncate_at_sentence(content: str) -> str:
    """상한을 넘으면 문장 경계에서 자른다. 단어 중간에서 끊는 것보다 낫다."""
    if len(content) <= MAX_CHARS:
        return content
    head = content[:MAX_CHARS]
    cut = max(head.rfind("다."), head.rfind(".\n"), head.rfind("\n\n"))
    return (head[: cut + 1] if cut > MAX_CHARS // 2 else head).rstrip()


def tokens(text: str) -> set[str]:
    """내용어만 남긴 토큰 집합. 조사·어미를 꼬리에서 떼어 근사한다."""
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        w = raw.lower()
        if w.isascii() or w.isdigit():
            out.add(w)
            continue
        for tail in _PARTICLES:
            if len(w) > len(tail) + 1 and w.endswith(tail):
                w = w[: -len(tail)]
                break
        if len(w) >= 2 and w not in _STOPWORDS:
            out.add(w)
    return out


def defects(draft: str, question: str, n_evidence: int = 0) -> list[str]:
    """규칙으로 잡히는 결함. 여기서 걸리면 리뷰를 부를 이유가 생긴다.

    `n_evidence` 는 검색이 실제로 건네준 근거 개수다. 0 이면 대괄호 번호는
    전부 지어낸 것이고, 3 이면 [4] 부터가 지어낸 것이다. 하네스 경로와
    raw 경로가 같은 함수를 쓰려면 이 값이 필요하다.
    """
    found: list[str] = []
    body = draft.strip()

    if not body:
        return ["answer is empty"]
    if len(body) < 40:
        found.append(f"answer is suspiciously short ({len(body)} chars)")

    # 잘림 — max_tokens 에 걸리면 문장 중간에서 끝난다.
    if not looks_closed(body):
        found.append("answer ends mid-sentence (likely truncated)")

    # 반복 붕괴
    if _RUN_RE.search(body):
        found.append("a character repeats pathologically")
    lines = [
        ln.strip()
        for ln in body.splitlines()
        if len(ln.strip()) > 12 and not _MD_RULE_RE.match(ln.strip())
    ]
    if lines:
        most = max(lines.count(ln) for ln in set(lines))
        if most >= _DUP_LINE_MIN:
            found.append(f"a whole line repeats {most} times")

    # 언어 불일치 — 한국어로 물었는데 한글이 거의 없다.
    q_ko = len(_HANGUL_RE.findall(question))
    a_ko = len(_HANGUL_RE.findall(body))
    if q_ko >= 3 and a_ko < max(5, len(body) // 20):
        found.append("question is Korean but the answer is not")

    # 건네지 않은 근거를 인용하고 있는가. 근거가 0개면 대괄호 번호는 전부 창작이다.
    cited = {int(m) for m in _CITE_RE.findall(body)}
    invented = sorted(n for n in cited if n < 1 or n > n_evidence)
    if invented:
        found.append(
            f"cites sources {invented} but only {n_evidence} pieces of evidence were given"
        )

    # 회피 — 상담 권유만 있고 실질 내용이 없다.
    if len(body) < 300 and _DEFLECT_RE.search(body):
        stripped = _DEFLECT_RE.sub("", body)
        if len(stripped) < 150:
            found.append("defers to a professional instead of answering")

    return found


# ── 2단계: 적합성 신호 (0 호출) ───────────────────────────────
def coverage_gaps(draft: str, question: str) -> list[str]:
    """질문이 물은 것을 답변이 다뤘는지에 대한 **의심 신호**.

    판정이 아니다. 여기서 걸리면 L2 에게 물어볼 뿐이고, 최종 판단은 L2 가 한다.
    형태소 분석기 없이 접미 제거로 근사하므로 단독으로는 신뢰할 수 없다.
    """
    gaps: list[str] = []
    q_tokens = tokens(question)
    if not q_tokens:
        return gaps
    a_tokens = tokens(draft)

    missing = sorted(q_tokens - a_tokens)
    hit = len(q_tokens & a_tokens) / len(q_tokens)
    # 질문의 내용어가 절반도 답변에 안 나오면 딴 얘기를 하고 있을 확률이 높다.
    if hit < 0.5:
        gaps.append(
            f"answer covers only {hit:.0%} of the question's key terms "
            f"(absent: {', '.join(missing[:6])})"
        )

    # 질문이 여러 개인데 답이 짧다 — 하나만 답했을 확률이 높다.
    asks = question.count("?") + question.count("？")
    asks += len(re.findall(r"(그리고|또한|또|그럼|아울러)\s", question))
    if asks >= 2 and len(draft) < 250 * asks:
        gaps.append(f"the question asks {asks} things but the answer is short")

    return gaps


# ── 3단계: L2 리뷰 ────────────────────────────────────────────
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "revise"]},
        "unanswered": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "final": {"type": "string"},
    },
    "required": ["verdict", "unanswered", "issues", "final"],
    "additionalProperties": False,
}

REVIEW_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "review", "schema": REVIEW_SCHEMA},
}

REVIEW_PROMPT = """You are the last reviewer before this answer reaches a real person.

Judge two things, in this order.

1. COVERAGE — does the answer actually answer what was asked?
   List in "unanswered" every distinct thing the question asked that the draft does
   not address. If the question asked several things, check each one separately.
   An answer that is fluent but addresses a different question is a failure.

2. SOUNDNESS — is anything in it wrong or malformed?
   Look for: statements that are factually wrong or unsafe; a dose, code, price,
   article number or guideline that reads invented; reasoning tokens or markup that
   leaked in; a sentence that stops mid-thought; the same content repeated; a
   bracketed citation that points at evidence that was not given;
   deferring to a professional instead of answering something answerable.

Then produce "final".
- If the draft is already good, set verdict "ok" and copy the draft into "final" unchanged.
- Otherwise set verdict "revise" and write the corrected answer in full into "final".
  Fix what is wrong and add what was missing. Keep everything that was already right —
  do not summarise, do not shorten, do not restructure for style alone.
- "final" must be the complete answer the person will read. Never leave it empty,
  never write a note about what you would change.
- Answer in {lang_name}.
{cite_rule}
{defect_note}{evidence_block}
QUESTION:
{question}

DRAFT:
{draft}"""

_NO_EVIDENCE_RULE = (
    "- No sources were retrieved for this answer, so it must not carry bracketed "
    "citation markers at all. Remove any that are there."
)
_EVIDENCE_RULE = (
    "- Cite as [1]..[{n}] ONLY the numbered evidence below, and never a number outside "
    "that range. If the evidence does not settle the question, say so plainly."
)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ```json 펜스나 앞말이 붙어 오는 경우 — 가장 바깥 중괄호만 떼어 본다.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


async def review(
    draft: str,
    question: str,
    call_fm,
    lang: str = "ko",
    deadline=None,
    mode: str | None = None,
    n_evidence: int = 0,
    evidence: str = "",
) -> tuple[str, list[str]]:
    """초안을 검사하고 필요하면 고쳐서 돌려준다. (최종 답변, 발견된 문제) 를 준다.

    어떤 실패에서도 초안보다 나쁜 것을 돌려주지 않는다.
    """
    mode = (mode or REVIEW_MODE).strip().lower()
    if mode == "off":
        return draft, []

    content, leaked = strip_think(draft)
    if not content:
        # 걷어냈더니 아무것도 없다. 원본이라도 내보낸다 — 여기서 빈 답을 만들면 0점이다.
        return draft.strip(), ["draft was empty after stripping reasoning tokens"]

    found = defects(content, question, n_evidence)
    if leaked:
        found.insert(0, "reasoning tokens leaked into the answer (removed locally)")

    # 커버리지 신호는 언제나 계산해 보고하되, 트리거로 쓸지는 스위치가 정한다.
    gaps = coverage_gaps(content, question)
    found += gaps
    triggers = found if REVIEW_COVERAGE else [f for f in found if f not in gaps]
    suspicious = bool(triggers)
    if mode != "always" and not suspicious:
        # 가장 흔한 경로다. FM 호출 0회로 끝난다.
        # 트리거가 아닌 신호도 그대로 돌려준다 — 로그에 남아야 나중에 A/B 로 쓴다.
        return _truncate_at_sentence(content), found

    if deadline is not None and deadline.expired(reserve=REVIEW_RESERVE_S):
        log.info("시간 예산으로 리뷰 호출 생략 (남은 %.0fs · 신호 %d개)",
                 deadline.remaining(), len(found))
        return _truncate_at_sentence(content), found

    prompt = REVIEW_PROMPT.format(
        lang_name="Korean" if lang == "ko" else "English",
        question=question[:2000],
        draft=content,
        cite_rule=(
            _EVIDENCE_RULE.replace("{n}", str(n_evidence))
            if n_evidence > 0
            else _NO_EVIDENCE_RULE
        ),
        evidence_block=(
            "\nEVIDENCE GIVEN TO THE WRITER:\n" + evidence.strip()[:4000] + "\n"
            if n_evidence > 0 and evidence.strip()
            else ""
        ),
        defect_note=(
            "\nA mechanical pre-check already flagged these — verify each one yourself,\n"
            "it can be wrong: " + "; ".join(found) + "\n"
            if found
            else ""
        ),
    )

    try:
        data = await call_fm(
            [{"role": "user", "content": prompt}],
            REVIEW_MAX_TOKENS,
            {
                "response_format": REVIEW_RESPONSE_FORMAT,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=answer_timeout(deadline, REVIEW_CAP_S, REVIEW_FLOOR_S),
        )
        choice = data["choices"][0]
        parsed = _extract_json(choice["message"].get("content") or "")
        if choice.get("finish_reason") == "length":
            # 고친 답이 잘렸다. 잘린 수정본은 초안보다 나쁘다.
            log.warning("리뷰 응답이 잘렸다 — 초안 유지")
            parsed = None
    except Exception as e:  # noqa: BLE001 — 검증이 죽어도 답변은 나가야 한다
        log.warning("리뷰 호출 실패: %s: %s", type(e).__name__, str(e)[:300])
        parsed = None

    if not parsed:
        return _truncate_at_sentence(content), found

    issues = [str(i) for i in (parsed.get("issues") or [])]
    unanswered = [str(i) for i in (parsed.get("unanswered") or [])]
    reported = found + [f"unanswered: {u}" for u in unanswered] + issues

    final, _ = strip_think(str(parsed.get("final") or "").strip())
    if parsed.get("verdict") != "revise" or not final:
        return _truncate_at_sentence(content), reported

    # 고친 답이 초안의 절반도 안 되면 요약해 버린 것이다. 완전성은 채점 축에서
    # 4% 지만, 있던 내용을 잃는 것은 정확성 축에서 그대로 손해다.
    if len(final) < len(content) // 2:
        log.warning("리뷰가 답을 %d→%d자로 줄여 초안을 유지한다", len(content), len(final))
        return _truncate_at_sentence(content), reported

    # 고친 답도 규칙 검사를 통과해야 한다. 통과 못 하면 초안이 낫다.
    if defects(final, question, n_evidence):
        log.warning("수정본이 규칙 검사를 통과하지 못해 초안을 유지한다")
        return _truncate_at_sentence(content), reported

    log.info("리뷰 수정 적용 — 미답변 %d · 지적 %d", len(unanswered), len(issues))
    return _truncate_at_sentence(final), reported
