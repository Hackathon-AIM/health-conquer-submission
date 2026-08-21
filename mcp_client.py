"""Lunit MCP 서버 클라이언트 — Streamable HTTP.

실측으로 확인한 것:
  - 세션 ID 가 필요 없다. initialize 핸드셰이크 없이 tools/call 이 바로 된다
  - 응답이 SSE 라 줄머리 `data: ` 를 벗겨야 JSON 이 된다
  - 도구 결과는 content[0].text 에 JSON 문자열로 들어온다
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("mcp")

MCP_URL = os.environ.get("LUNIT_MCP_URL", "https://mcp.hackathon.lunit.io/mcp")
MCP_KEY = os.environ.get("LUNIT_FM_API_KEY", "")
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "60"))
MCP_RETRIES = int(os.environ.get("MCP_RETRIES", "3"))

# 같은 도구·같은 인자를 다시 부르지 않는다.
#
# CoEval 은 멀티턴에서 매 턴 대화 전체를 다시 보낸다. 서버가 무상태라 3턴 대화는
# 같은 근거를 세 번 검색한다 — 상류 호출이 그대로 3배다. 여기에 TTL 캐시를 두면
# 2·3턴의 검색이 통째로 사라진다.
#
# 0 이면 끈다(기본, A/B 용). 켤 때 TTL 은 평가 한 회차보다 짧게 잡는다.
MCP_CACHE_TTL_S = float(os.environ.get("MCP_CACHE_TTL_S", "0"))
MCP_CACHE_MAX = int(os.environ.get("MCP_CACHE_MAX", "512"))

RETRY_STATUS = {429, 500, 502, 503, 504}


def _parse_sse(text: str) -> dict[str, Any] | None:
    """`event: message\\ndata: {...}` 에서 JSON 을 꺼낸다. 평범한 JSON 응답도 받아준다."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


class MCPClient:
    def __init__(self, url: str = MCP_URL, key: str = MCP_KEY, concurrency: int = 6) -> None:
        self.url = url
        self.key = key
        self._tools: list[dict] | None = None
        self._lock = asyncio.Lock()
        # 연결을 재사용한다. 호출마다 새 클라이언트를 만들면 소켓이 쌓인다.
        self._client = httpx.AsyncClient(
            timeout=MCP_TIMEOUT,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
        self._sem = asyncio.Semaphore(concurrency)
        # key -> (만료시각, 결과). 프로세스 안에서만 산다.
        self._cache: dict[str, tuple[float, Any]] = {}
        self.cache_hits = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(
        self, method: str, params: dict | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.key}",
        }
        # timeout 은 재시도·백오프까지 포함한 이 호출 전체의 상한이다.
        # 안 주면 예전대로 시도마다 MCP_TIMEOUT(기본 60초)을 쓴다 — 요청 예산이
        # 40초인데 도구 하나가 60초를 쓸 수 있었다는 뜻이다.
        total = timeout if (timeout and timeout > 0) else MCP_TIMEOUT * MCP_RETRIES
        started = time.monotonic()

        def _left() -> float:
            return total - (time.monotonic() - started)

        last: Exception | None = None
        for attempt in range(MCP_RETRIES):
            if _left() <= 0:
                log.warning("MCP 시간 예산 소진 — %d/%d 시도에서 중단", attempt + 1, MCP_RETRIES)
                break
            try:
                async with self._sem:
                    left = _left()
                    if left <= 0:
                        break
                    r = await self._client.post(
                        self.url, headers=headers, json=body, timeout=min(MCP_TIMEOUT, left)
                    )
                if r.status_code == 200:
                    parsed = _parse_sse(r.text)
                    if parsed is None:
                        raise ValueError(f"파싱 실패: {r.text[:200]}")
                    if "error" in parsed:
                        # 프로토콜 레벨 에러는 재시도해도 같다.
                        raise ValueError(f"MCP error: {str(parsed['error'])[:300]}")
                    return parsed.get("result", {})
                log.warning("MCP %d (%d/%d): %s", r.status_code, attempt + 1, MCP_RETRIES,
                            r.text[:200])
                if r.status_code not in RETRY_STATUS:
                    r.raise_for_status()
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request,
                                             response=r)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning("MCP 통신 실패 (%d/%d): %s", attempt + 1, MCP_RETRIES, type(e).__name__)
                last = e
            if attempt < MCP_RETRIES - 1:
                backoff = 1.5 * (2**attempt)
                if _left() <= backoff:
                    log.warning("MCP 재시도 포기 — 남은 예산 %.1fs", _left())
                    break
                await asyncio.sleep(backoff)
        if last is None:
            raise TimeoutError(f"MCP 호출이 시간 예산 {total:.0f}s 안에 시작되지 못했다")
        raise last

    async def list_tools(self, timeout: float | None = None) -> list[dict]:
        """도구 목록. 한 번 받아두고 재사용한다 — 매 턴 부를 이유가 없다.

        여기에도 상한이 필요하다. MCP 가 죽어 있으면 이 호출이 재시도까지
        MCP_TIMEOUT×MCP_RETRIES(기본 180초)를 쓰고, 그동안 요청은 검색을
        시작조차 못 한 채 예산을 통째로 날린다.
        """
        async with self._lock:
            if self._tools is None:
                self._tools = (await self._rpc("tools/list", timeout=timeout)).get("tools", [])
                log.info("MCP 도구 %d개 로드", len(self._tools))
            return self._tools

    def _cache_key(self, name: str, arguments: dict) -> str:
        return name + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    async def call_tool(
        self, name: str, arguments: dict, timeout: float | None = None
    ) -> Any:
        """도구를 부르고 결과를 파이썬 객체로 돌려준다.

        서버는 content[0].text 에 JSON 문자열을 담아 준다. JSON 이 아니면 원문 그대로.
        `timeout` 은 재시도까지 포함한 이 호출 전체의 상한이다.
        """
        key = ""
        if MCP_CACHE_TTL_S > 0:
            key = self._cache_key(name, arguments)
            hit = self._cache.get(key)
            if hit is not None:
                expires, payload = hit
                if expires > time.monotonic():
                    self.cache_hits += 1
                    log.info("MCP 캐시 적중 %s (누적 %d)", name, self.cache_hits)
                    return payload
                self._cache.pop(key, None)

        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and "result" in sc:
            payload: Any = sc["result"]
        else:
            content = result.get("content") or []
            if content and isinstance(content[0], dict):
                text = content[0].get("text", "")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = text
            else:
                payload = result

        if key:
            # 실패는 캐시하지 않는다 — 일시적 실패를 TTL 동안 굳혀 버리면
            # 재시도가 통째로 무의미해진다.
            if len(self._cache) >= MCP_CACHE_MAX:
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = (time.monotonic() + MCP_CACHE_TTL_S, payload)
        return payload
