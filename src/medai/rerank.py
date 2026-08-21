"""L3 · 리랭킹 + 컨텍스트 조립.

넓게 뽑고(top 20~50) → 크로스인코더로 정밀 재정렬 → top-5만 남긴다.

왜 두 단계인가
  벡터 검색은 질문과 문서를 '따로' 임베딩해 비교하지만,
  리랭커는 '둘을 붙여서 한 번에' 읽는다. 훨씬 정확하지만 느려서 후보 전체엔 못 돌린다.
  → "넓게 뽑고 정밀하게 거른다"가 정석. 투입 대비 효과가 가장 큰 구간이다.

컨텍스트 예산을 고정하는 이유
  안 그러면 검색 결과가 많은 날엔 프롬프트가 부풀어 응답 품질이 흔들리고 지연도 튄다.
  ⚠️ 모델 컨텍스트가 8K면 예산을 2400 이하로 잡을 것 (config.retrieval.context_token_budget).

출처 태깅을 하는 이유
  ① 답변에 근거를 인용하면 루브릭 가점
  ② L4c 비평 패스가 "이 주장에 근거가 있나"를 검증할 수 있다 (groundedness)
"""

from __future__ import annotations

from .config import Config
from .contracts import Context, Doc

try:
    from FlagEmbedding import FlagReranker  # type: ignore
except Exception:
    FlagReranker = None  # type: ignore

_RERANKER = None


def preload(cfg) -> bool:
    """리랭커를 미리 로딩한다. Pipeline 생성 시 한 번만 호출.

    ⚠️ 지연 로딩 금지
      FlagReranker 로딩은 동기 작업이라 async 실행 중에 부르면 이벤트 루프를 통째로 막는다.
      실제로 첫 턴 지연이 27ms -> 89초로 튄 사고가 있었다.
      비용을 낼 거면 시작할 때 눈에 보이게 내고, 실행 중에는 내지 않는다.
    """
    global _RERANKER
    if cfg["retrieval"].get("reranker", "none") != "bge":
        _RERANKER = False
        return False
    if FlagReranker is None:
        print("  [rerank] FlagEmbedding 미설치 -> 점수 기반 정렬로 폴백")
        _RERANKER = False
        return False
    if _RERANKER is None:
        name = cfg["retrieval"].get("reranker_model", "BAAI/bge-reranker-v2-m3")
        print(f"  [rerank] 로딩 중: {name} (최초 1회, 수십 초 걸릴 수 있음)")
        try:
            _RERANKER = FlagReranker(name, use_fp16=True)
            print("  [rerank] 준비 완료")
        except Exception as e:
            print(f"  [rerank] 로딩 실패 -> 점수 기반 정렬로 폴백: {e}")
            _RERANKER = False
    return bool(_RERANKER)


def _get_reranker():
    """준비된 리랭커만 돌려준다. 여기서 새로 로딩하지 않는다."""
    return _RERANKER or None


def _tok(s: str) -> int:
    """대략적 토큰 수. 한국어는 글자당 ~0.7토큰으로 잡는다.

    ⚠️ 정확한 값이 필요하면 모델 토크나이저로 교체할 것.
       Gravity 계열은 GLM-4.5 기반 토크나이저(151,552 vocab)라 한국어 효율이 괜찮다.
    """
    return int(len(s) * 0.7) + 1


def rerank(query: str, docs: list[Doc], top_k: int) -> list[Doc]:
    if not docs:
        return []

    # 후보가 뽑을 개수 이하면 리랭킹할 게 없다.
    # (mock 은 소스당 1~3건이라 항상 여기서 끝난다)
    if len(docs) <= top_k:
        return sorted(docs, key=lambda d: -d.score)

    rr = _get_reranker()
    if rr is not None:
        try:
            pairs = [[query, f"{d.title} {d.text}"] for d in docs]
            scores = rr.compute_score(pairs, normalize=True)
            if isinstance(scores, float):
                scores = [scores]
            for d, s in zip(docs, scores):
                d.score = float(s)
        except Exception:
            pass

    # 소스 다양성 보정 — 한 소스가 top-k를 독식하지 않게
    ranked = sorted(docs, key=lambda d: -d.score)
    out: list[Doc] = []
    per_source: dict[str, int] = {}
    cap = max(1, top_k // 2)
    for d in ranked:
        if len(out) >= top_k:
            break
        if per_source.get(d.source, 0) >= cap and len(out) < top_k - 1:
            continue
        out.append(d)
        per_source[d.source] = per_source.get(d.source, 0) + 1
    for d in ranked:                       # 자리가 남으면 채운다
        if len(out) >= top_k:
            break
        if d not in out:
            out.append(d)
    return out[:top_k]


def assemble(query: str, docs: list[Doc], cfg: Config) -> Context:
    rc = cfg["retrieval"]
    top = rerank(query, docs, int(rc.get("rerank_top_k", 5)))

    budget = int(rc.get("context_token_budget", 2400))
    kept: list[Doc] = []
    used = 0
    dropped = 0
    for d in top:
        cost = _tok(d.cite() + " " + d.text)
        if used + cost > budget:
            dropped += 1
            continue
        kept.append(d)
        used += cost

    # 조용한 절삭 금지 — 무엇이 잘렸는지 기록한다
    return Context(docs=kept, tokens_used=used, dropped=dropped + max(0, len(docs) - len(top)))
