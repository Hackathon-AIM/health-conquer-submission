"""L3 · 의약품 정보 (식약처 허가사항 / e약은요).

⚠️ 여기는 BM25 키워드 매칭이 생명이다.
   순수 벡터 검색만 쓰면 "타이레놀"과 "타이레놀 콜드"를 비슷하다고 판단해
   용량 조언이 조용히 틀린다. 콜드엔 다른 성분이 더 들어 있다.

⭐ risk_check의 데이터 원천
   허가사항의 '사용상의 주의사항' 필드에 음주·임신·간/신장 관련 서술이 들어 있다.
   DUR 8종은 약-약 / 약-환자속성만 다루고 약-알코올은 아예 없으므로,
   이 필드를 파싱해서 gates/risk.py 의 규칙 테이블을 만든다.
"""

from __future__ import annotations

from ..contracts import Doc
from .base import Searcher


class DrugSearcher(Searcher):
    name = "drug"

    # TODO(현장): 오프닝에서 받은 값으로 교체
    ENDPOINT = ""
    CAUTION_FIELD = "사용상의주의사항"   # TODO(현장): 실제 필드명 확인

    async def _search(self, query: str, top_k: int) -> list[Doc]:
        if not self.ENDPOINT or self.client is None:
            return []

        payload = {"query": query, "top_k": top_k}
        r = await self.client.post(self.ENDPOINT, json=payload)
        r.raise_for_status()
        data = r.json()

        items = data.get("results") or data.get("items") or []
        out: list[Doc] = []
        for it in items:
            out.append(Doc(
                source=self.name,
                title=str(it.get("제품명") or it.get("title", "")),
                text=str(it.get("text") or it.get("효능효과") or ""),
                url=it.get("url"),
                locator=it.get("품목기준코드") or it.get("성분코드"),
                score=float(it.get("score", 0.0)),
            ))
        return out

    async def fetch_caution(self, drug_name: str) -> str:
        """단일 약물의 '사용상의 주의사항' 원문. risk_check 규칙 생성에 사용."""
        if not self.ENDPOINT or self.client is None:
            return ""
        docs = await self._search(drug_name, 1)
        return docs[0].text if docs else ""
