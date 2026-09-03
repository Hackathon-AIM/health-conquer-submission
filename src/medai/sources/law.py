"""L3 · 건강보험 법률 + 심평원 급여기준.

조·항·호 단위로 조회해야 한다. 조문 번호로 인용하면 신뢰도가 붙는다.
  예) "국민건강보험법 제41조에 따르면…"

⚠️ 이 테마는 HealthBench에 거의 없다 → 벤치마크 점수 기여는 적다.
   하지만 프론티어 상(임상의 블라인드)에선 강력한 차별화다.
   GPT/Claude는 한국 건보 세부 규정을 모른다.
   → 투자 우선순위는 낮게, 하지만 반드시 넣을 것.

법 해석을 단정하면 위험하므로 "제도 안내 + 공단·심평원 확인 권유" 템플릿으로 고정.
"""

from __future__ import annotations

from ..contracts import Doc
from .base import Searcher


class LawSearcher(Searcher):
    name = "law"
    ENDPOINT = ""          # TODO(현장)

    async def _search(self, query: str, top_k: int) -> list[Doc]:
        if not self.ENDPOINT or self.client is None:
            return []
        r = await self.client.post(self.ENDPOINT, json={"query": query, "top_k": top_k})
        r.raise_for_status()
        items = (r.json().get("results") or [])
        return [
            Doc(
                source=self.name,
                title=str(it.get("법령명") or it.get("title", "")),
                text=str(it.get("조문내용") or it.get("text", "")),
                url=it.get("url"),
                locator=it.get("조문번호") or it.get("article"),
                score=float(it.get("score", 0.0)),
            )
            for it in items
        ]


class HiraSearcher(LawSearcher):
    """심평원 요양급여 적용기준 — 어떤 조건에서 급여가 인정되는지."""
    name = "hira"
    ENDPOINT = ""          # TODO(현장)
