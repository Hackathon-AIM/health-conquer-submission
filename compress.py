"""근거 압축 — 가져온 것 중 **무엇을 넣을지** 고른다.

문제
  MCP 도구 하나가 돌려주는 양이 크다. 실측(라이브):
      index_get_page_content 20페이지   29.4 KB
      index_get_page_content  5페이지   24.1 KB
      hira_updates_search  limit=10     26.4 KB
      rag_vector_query     top_k=10     22.5 KB
      adr_retrieve_drug_info             9.8 KB
  검색 한 번에 서너 개를 부르면 수십 KB 가 되고, 그걸 그대로 생성 프롬프트에
  얹을 수는 없다.

왜 못 얹는가 — 실측으로 확인한 진짜 이유
  게이트웨이 입력 상한은 바이트 추정 기반 약 384~400KB(≈50,000 추정토큰)다.
  256KB 는 통과하고(prompt_tokens 32,783) 400KB 에서 input_limit_exceeded 가 난다.
  즉 하드 상한에는 여유가 있다. 정작 우리를 막는 것은 셋이다.

    1. 시간.   요청 예산이 40초다. 입력이 길수록 prefill 이 길어지고, 그만큼
               답을 쓸 시간이 사라진다. 늦은 답은 없는 답과 같게 채점된다.
    2. 출력 예산. 사고 토큰이 max_tokens 의 46~50% 를 먹는다(실측). CoEval 의
               conquer_val 설정은 "RAG 컨텍스트가 있으면 그 비중이 더 올라간다"
               고 적는다. 근거를 늘리면 답변 몫이 줄어든다.
    3. 위치 편향. 긴 맥락의 가운데 정보는 덜 쓰인다("lost in the middle").
               넣었다고 읽히는 것이 아니다.

  그래서 목표는 "많이 넣기" 가 아니라 **"질문에 답하는 데 쓰이는 것만 넣기"** 다.

무엇을 참고했나
  · Contextual Compression in RAG (Survey, arXiv:2409.13385) — 압축을 어휘 선택 /
    추상 요약 / 재순위 세 갈래로 정리한다.
  · RECOMP — 검색 결과를 생성 전에 요약해 넣는다. 요약에 모델을 한 번 더 쓴다.
  · Provence (arXiv:2501.16214) — 질의 조건부로 **문장 단위 가지치기**. 재순위
    단계에 붙여 추가 비용을 거의 0 으로 만든다. 높은 압축률에서도 성능이 거의
    안 떨어지는 유일한 방법이라고 보고한다.
  · Squeez (arXiv:2604.04979) — 에이전트의 **도구 출력**을 과제 조건부로 가지치기.
    우리 상황과 가장 가깝다: 도구가 뱉는 장황한 덩어리에서 과제에 쓰이는 조각만 남긴다.
  · Lost in the Middle 계열 — 중요한 것을 앞과 뒤에 둔다.

여기서 고른 방법과 그 이유
  RECOMP 처럼 LLM 으로 요약하면 호출이 한 번 더 늘어난다. 이 저장소의 실측 이력이
  일관되게 말하는 것은 **요청당 호출 수가 곧 점수**라는 것이다(느려서 0.00 을 받은
  적이 있다). 그래서 Provence·Squeez 쪽 — **질의 조건부 추출식 가지치기** — 를
  택하되, 학습된 모델 없이 결정론적으로 한다. LLM 호출 0회, 수 밀리초.

  1. 문단 단위로 쪼갠다.
  2. 질의어와 겹치는 정도로 점수를 매긴다(숫자·단위·영문 용어에 가중치).
  3. 예산 안에서 높은 것부터 담되, **원문 순서를 유지**해 문맥을 깨지 않는다.
  4. 항목 간에는 균등 배분한다 — 한 항목이 20페이지라고 나머지를 굶기지 않는다.
  5. 중복 문단은 버린다(같은 페이지가 도구 여러 개에서 겹쳐 온다).
  6. 가장 관련 있는 항목을 맨 앞에, 그다음을 맨 뒤에 둔다.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

# 문단 경계. 표·목록이 많아 빈 줄과 단독 줄바꿈을 모두 본다.
_PARA_SPLIT = re.compile(r"\n\s*\n|\r\n\s*\r\n")
_HANGUL = re.compile(r"[가-힣]")
# 점수 가중 대상: 숫자(용량·연도·조문), 단위, 코드, 영문 용어.
_NUMERIC = re.compile(r"\d")
_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z\-]{2,}|\d+(?:[.,]\d+)?")

_PARTICLES = (
    "으로부터", "에서는", "에게서", "이라는", "라는", "에서", "에게", "으로", "까지", "부터",
    "이나", "나요", "까요", "인가", "는지", "은지", "하고", "와의", "과의",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "로", "와", "과", "요",
)


def tokens(text: str) -> set[str]:
    """내용어 토큰. 조사를 꼬리에서 떼어 근사한다 — 판정이 아니라 점수용이다."""
    out: set[str] = set()
    for raw in _TOKEN.findall(text or ""):
        w = raw.lower()
        if not _HANGUL.search(w):
            out.add(w)
            continue
        for tail in _PARTICLES:
            if len(w) > len(tail) + 1 and w.endswith(tail):
                w = w[: -len(tail)]
                break
        if len(w) >= 2:
            out.add(w)
    return out


def paragraphs(text: str, min_len: int = 30) -> list[str]:
    """문단으로 쪼갠다. 너무 짧은 조각은 앞 문단에 붙인다 — 표 머리글이 홀로 남지 않게."""
    parts = [p.strip() for p in _PARA_SPLIT.split(text or "") if p.strip()]
    if not parts:
        return []
    out: list[str] = []
    for p in parts:
        if out and len(p) < min_len:
            out[-1] = out[-1] + "\n" + p
        else:
            out.append(p)
    return out


def score_paragraph(para: str, q_tokens: set[str]) -> float:
    """질의어와 얼마나 겹치는가. 숫자가 든 문단을 조금 올린다.

    의료 질문에서 답을 결정하는 것은 대개 용량·연령·기준치·조문 번호처럼
    숫자가 든 문장이다. 같은 점수면 그쪽을 남기는 편이 낫다.
    """
    if not q_tokens:
        return 0.0
    p_tokens = tokens(para)
    if not p_tokens:
        return 0.0
    hit = len(q_tokens & p_tokens)
    if hit == 0:
        return 0.0
    # 길이로 나눠 장황한 문단이 겹침 수만으로 이기지 않게 한다.
    density = hit / (len(p_tokens) ** 0.5)
    if _NUMERIC.search(para):
        density *= 1.25
    return density


def prune_text(text: str, query: str, budget: int) -> tuple[str, int]:
    """질의에 맞는 문단만 예산 안에서 남긴다. (남긴 텍스트, 버린 문단 수)

    원문 순서를 유지한다. 점수 순으로 이어붙이면 문맥이 깨진다.
    """
    text = (text or "").strip()
    if len(text) <= budget:
        return text, 0
    paras = paragraphs(text)
    if not paras:
        return text[:budget], 0
    q = tokens(query)
    scored = sorted(
        ((score_paragraph(p, q), i, p) for i, p in enumerate(paras)),
        key=lambda x: (-x[0], x[1]),
    )
    picked: list[tuple[int, str]] = []
    used = 0
    for s, i, p in scored:
        cost = len(p) + 1
        if used + cost > budget:
            # 첫 문단조차 예산을 넘으면 그것만 잘라서라도 넣는다 — 빈손보다 낫다.
            if not picked:
                return p[:budget], len(paras) - 1
            continue
        picked.append((i, p))
        used += cost
    picked.sort()
    dropped = len(paras) - len(picked)
    joined = "\n\n".join(p for _, p in picked)
    if dropped:
        joined += f"\n[... {dropped} more paragraphs omitted ...]"
    return joined, dropped


def _fingerprint(text: str) -> str:
    """중복 판정용 지문. 공백·구두점을 지운 **전문**의 해시다.

    앞부분만 보면 안 된다. 같은 문서의 앞 몇 문단이 같고 뒤가 다른 경우가 흔한데
    — 예를 들어 같은 보고서의 서로 다른 페이지 구간 — 그걸 중복으로 지우면
    정작 답이 든 쪽을 잃는다. 실제로 이 함정에 걸린 테스트를 봤다.
    정확히 같은 것만 지운다.
    """
    norm = re.sub(r"[\s\W]+", "", text or "")
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def pack(
    items: Iterable, query: str, budget: int, per_item_cap: int = 1400
) -> list:
    """근거 항목들을 예산 안에 담는다. 담긴 것만 원래 순서 정보와 함께 돌려준다.

    받는 것은 `.text`, 선택적으로 `.relevance` 를 가진 객체들이다.
    돌려주는 것은 (원본항목, 압축된 텍스트) 목록이며, **가장 관련 있는 것이 맨 앞,
    그다음이 맨 뒤**에 오도록 재배치돼 있다 — 긴 맥락의 가운데는 덜 읽힌다.
    """
    items = list(items)
    if not items:
        return []

    # 1) 중복 제거. 같은 페이지가 도구 여러 개에서 겹쳐 오는 것을 실측으로 봤다.
    seen: set[str] = set()
    uniq = []
    for it in items:
        fp = _fingerprint(getattr(it, "text", ""))
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        uniq.append(it)

    # 2) 균등 배분. 한 항목이 20페이지라고 나머지를 굶기지 않는다.
    share = max(300, min(per_item_cap, budget // max(1, len(uniq))))

    packed: list[tuple[object, str]] = []
    used = 0
    for it in uniq:
        room = min(share, budget - used)
        if room < 200:
            break
        text, _ = prune_text(getattr(it, "text", ""), query, room)
        if not text:
            continue
        packed.append((it, text))
        used += len(text)

    # 3) 위치 배치: 1등을 맨 앞, 2등을 맨 뒤로. 가운데가 가장 덜 읽힌다.
    if len(packed) >= 3:
        order = sorted(
            range(len(packed)),
            key=lambda i: -float(getattr(packed[i][0], "relevance", 0.0) or 0.0),
        )
        first, last = order[0], order[1]
        middle = [i for i in range(len(packed)) if i not in (first, last)]
        packed = [packed[first]] + [packed[i] for i in middle] + [packed[last]]

    return packed
