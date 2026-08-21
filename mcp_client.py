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
from typing import Any

import httpx

log = logging.getLogger("mcp")

MCP_URL = os.environ.get("LUNIT_MCP_URL", "https://mcp.hackathon.lunit.io/mcp")
MCP_KEY = os.environ.get("LUNIT_FM_API_KEY", "")
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "60"))
MCP_RETRIES = int(os.environ.get("MCP_RETRIES", "3"))

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
    def __init__(self, url: str = MCP_URL, key: str = MCP_KEY) -> None:
        self.url = url
        self.key = key
        self._tools: list[dict] | None = None
        self._lock = asyncio.Lock()

    async def _rpc(self, method: str, params: dict | None = None) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.key}",
        }
        last: Exception | None = None
        for attempt in range(MCP_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as c:
                    r = await c.post(self.url, headers=headers, json=body)
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
                await asyncio.sleep(1.5 * (2**attempt))
        assert last is not None
        raise last

    async def list_tools(self) -> list[dict]:
        """도구 목록. 한 번 받아두고 재사용한다 — 매 턴 부를 이유가 없다."""
        async with self._lock:
            if self._tools is None:
                self._tools = (await self._rpc("tools/list")).get("tools", [])
                log.info("MCP 도구 %d개 로드", len(self._tools))
            return self._tools

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """도구를 부르고 결과를 파이썬 객체로 돌려준다.

        서버는 content[0].text 에 JSON 문자열을 담아 준다. JSON 이 아니면 원문 그대로.
        """
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result
