"""L3 · 소스 레지스트리 + 병렬 디스패처.

라우팅 '결정'은 L2가 내리고, 여기는 그 결정을 '실행'만 한다.
디스패처는 컴포넌트라고 부를 것도 없이 몇 줄이다.
"""

from __future__ import annotations

import asyncio

from ..config import Config
from ..contracts import Doc
from .base import Searcher, make_http_client
from .drug import DrugSearcher
from .guideline import GuidelineSearcher
from .law import HiraSearcher, LawSearcher
from .mock import MockSearcher
from .pubmed import PubmedSearcher

LIVE = {
    "guideline": GuidelineSearcher,
    "drug": DrugSearcher,
    "law": LawSearcher,
    "hira": HiraSearcher,
    "pubmed": PubmedSearcher,
}

ALL_NAMES = list(LIVE.keys())


def build(cfg: Config) -> dict[str, Searcher]:
    mode = cfg["retrieval"].get("mode", "mock")
    if mode == "mock":
        return {n: MockSearcher(cfg, n) for n in ALL_NAMES}
    client = make_http_client(cfg)
    return {n: cls(cfg, client) for n, cls in LIVE.items()}


async def search_all(
    searchers: dict[str, Searcher],
    sources: list[str],
    queries: dict[str, str],
    fallback_query: str,
    top_k: int,
) -> list[Doc]:
    """병렬 실행 — 순차면 최대 12초, 병렬이면 가장 느린 것 하나 값(3초).

    한 소스가 죽어도 나머지로 진행한다 (Searcher.search가 예외를 흡수).
    """
    if not sources:
        return []
    tasks = [
        searchers[s].search(queries.get(s) or fallback_query, top_k)
        for s in sources
        if s in searchers
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    docs: list[Doc] = []
    for r in results:
        if isinstance(r, list):
            docs.extend(r)
    return docs
