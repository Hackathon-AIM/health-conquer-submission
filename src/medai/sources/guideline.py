"""L3 · 진료 가이드라인 (KoMGI 등) — 증상/질환 질문의 기본 소스.

이 소스가 가장 가치가 높다:
  · PubMed 개별 논문보다 이미 전문가가 합의·정제한 결론이라 인용이 안전
  · HealthBench가 요구하는 '행동 가능한 다음 조치'(언제 병원, 어떤 검사)가 그대로 들어 있음
  · 만성질환은 일반 국민 질문의 큰 비중

검색 방식: 벡터 + 키워드 혼합 (개념 검색이 통함)
"""

from __future__ import annotations

from ..contracts import Doc
from .base import Searcher


class GuidelineSearcher(Searcher):
    name = "guideline"

    # TODO(현장): 오프닝에서 받은 값으로 교체
    ENDPOINT = ""          # 예: "https://.../search/guideline"
    METHOD = "POST"

    async def _search(self, query: str, top_k: int) -> list[Doc]:
        if not self.ENDPOINT or self.client is None:
            return []

        # TODO(현장): 요청 스키마 확인 후 수정
        payload = {"query": query, "top_k": top_k}
        r = await self.client.post(self.ENDPOINT, json=payload)
        r.raise_for_status()
        data = r.json()

        # TODO(현장): 응답 필드명 확인 후 수정
        items = data.get("results") or data.get("items") or data.get("data") or []
        return [
            Doc(
                source=self.name,
                title=str(it.get("title", "")),
                text=str(it.get("text") or it.get("content") or it.get("snippet") or ""),
                url=it.get("url"),
                locator=it.get("section") or it.get("page"),
                score=float(it.get("score", 0.0)),
            )
            for it in items
        ]
