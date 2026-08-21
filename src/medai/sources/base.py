"""L3 · 소스 래퍼 공통 규약.

담당: B

⚠️ 엔드포인트 스펙은 미공개다. 각 소스 파일의 TODO(현장) 부분만 채우면 된다.
   그 전까지 mode=mock 으로 A·C·D가 계속 작업할 수 있다.

현장 첫 60분 체크리스트
  [ ] 응답 스키마 · 필드명
  [ ] top_k 최대값
  [ ] rate limit — 병렬로 던져도 되는지
  [ ] 지연 (p50 / p95)
  [ ] 의약품 소스에 '사용상의 주의사항' 필드가 있는지 ← risk_check 데이터 원천
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from ..contracts import Doc

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


class Searcher(ABC):
    name: str = "base"

    def __init__(self, cfg, client: Any = None):
        self.cfg = cfg
        self.client = client
        self.rc = cfg["retrieval"]

    @abstractmethod
    async def _search(self, query: str, top_k: int) -> list[Doc]:
        ...

    async def search(self, query: str, top_k: int | None = None) -> list[Doc]:
        """타임아웃 · 예외를 여기서 흡수한다.

        한 소스가 죽어도 나머지로 진행해야 한다.
        PubMed 타임아웃 때문에 답변 전체를 실패시키면 안 된다.
        """
        k = top_k or int(self.rc.get("top_k_per_source", 20))
        try:
            return await asyncio.wait_for(
                self._search(query, k), timeout=float(self.rc.get("timeout", 8.0))
            )
        except Exception:
            return []


def make_http_client(cfg) -> Any:
    if httpx is None:
        return None
    return httpx.AsyncClient(
        timeout=float(cfg["retrieval"].get("timeout", 8.0)),
        headers={"accept": "application/json"},
    )
