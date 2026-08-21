"""L3 · PubMed — 근거층 (수평 단계가 아니라 가이드라인 아래 깔린 수직 층).

⚠️ 기본 OFF. 조건부로만 켠다.
   일반인 질문의 90%는 PubMed가 불필요하고, 켜면 노이즈와 지연만 늘어난다.
   "최신 연구 / 논란 / 근거" 신호가 있을 때만 L2가 extra_sources 로 요청한다.

함정 2가지
  1. 언어 — 사용자는 "혈압약 먹으면 어지러운데"라고 묻지만
     PubMed는 antihypertensive AND (dizziness OR orthostatic hypotension) 를 원한다.
  2. 개별 RCT 하나로 일반인에게 조언하면 위험하다.
     → Review / Meta-Analysis 필터가 사실상 필수. 그게 곧 가이드라인 수준의 근거.
"""

from __future__ import annotations

from ..contracts import Doc
from .base import Searcher

PUBTYPE_FILTER = "(Review[pt] OR Meta-Analysis[pt] OR Practice Guideline[pt])"


class PubmedSearcher(Searcher):
    name = "pubmed"
    ENDPOINT = ""          # TODO(현장). 자체 래핑이 없으면 NCBI E-utilities 직접 사용

    async def _search(self, query: str, top_k: int) -> list[Doc]:
        if not self.ENDPOINT or self.client is None:
            return []
        # query는 L3 전처리에서 이미 영문으로 재작성된 상태로 들어온다
        q = query if "[pt]" in query else f"({query}) AND {PUBTYPE_FILTER}"
        r = await self.client.post(self.ENDPOINT, json={"query": q, "top_k": top_k})
        r.raise_for_status()
        items = (r.json().get("results") or [])
        return [
            Doc(
                source=self.name,
                title=str(it.get("title", "")),
                text=str(it.get("abstract") or it.get("text", "")),
                url=it.get("url"),
                locator=f"PMID:{it.get('pmid')}" if it.get("pmid") else None,
                score=float(it.get("score", 0.0)),
            )
            for it in items
        ]
