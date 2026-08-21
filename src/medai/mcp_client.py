"""Lunit MCP 서버 클라이언트 — Streamable HTTP (JSON-RPC over POST).

대상: https://mcp.hackathon.lunit.io/mcp
인증: Authorization: Bearer $LUNIT_FM_API_KEY (팀 키 하나로 Model·Simulator·MCP 공용)

Codex 는 config.toml 로 붙지만, 우리 하네스(l2.py)는 파이썬에서 직접
tools/list · tools/call 을 불러야 하므로 최소 클라이언트를 만든다.
mcp 패키지를 안 쓰는 이유: 의존성 하나 = 사고 하나. 필요한 건 세 메서드뿐이다.

응답이 SSE(text/event-stream)로 올 수도, 일반 JSON으로 올 수도 있어 둘 다 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

import httpx

_CITE = re.compile(r"cite-[0-9a-f]{8,}", re.I)
_CITE_FIELD = re.compile(r'"cite_uid"\s*:\s*"([^"]+)"')

PROTOCOL_VERSION = "2025-03-26"


def find_cite_uids(text: str) -> list[str]:
    """tool 결과 텍스트에서 cite_uid 를 전부 뽑는다.

    가이드: '일부 MCP tool result 에는 cite_uid field 가 있다'.
    형식이 cite-3f9a1c7d2e5b8046 꼴로 예시에 나오므로 그 패턴 + JSON 필드 둘 다 잡는다.
    실제 형식은 현장에서 scripts/probe_mcp.py 로 확인하고 패턴이 다르면 여기만 고친다.
    """
    uids = list(dict.fromkeys(_CITE.findall(text)))
    for m in _CITE_FIELD.findall(text):      # JSON 파싱이 실패해도 필드는 잡는다
        if m not in uids:
            uids.append(m)
    try:
        obj = json.loads(text)
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                v = cur.get("cite_uid")
                if isinstance(v, str) and v not in uids:
                    uids.append(v)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    except Exception:
        pass
    return uids


class McpClient:
    def __init__(
        self,
        url: str = "https://mcp.hackathon.lunit.io/mcp",
        api_key_env: str = "LUNIT_FM_API_KEY",
        timeout: float = 60.0,          # Codex 권장 tool_timeout_sec=60 과 동일
    ):
        self.url = url
        self.key = os.getenv(api_key_env, "")
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._id = 0
        self._tools: Optional[list[dict]] = None
        self._lock = asyncio.Lock()

    # ── 저수준 ──────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # SSE: data: {...} 줄들 중 마지막 JSON-RPC 응답을 취한다
            last = None
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                        if "result" in obj or "error" in obj:
                            last = obj
                    except Exception:
                        continue
            if last is None:
                raise RuntimeError(f"SSE 응답에서 JSON-RPC 결과를 못 찾음: {resp.text[:300]}")
            return last
        return resp.json()

    async def _rpc(self, method: str, params: Optional[dict] = None,
                   *, notification: bool = False) -> Optional[dict]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._id += 1
            body["id"] = self._id
        resp = await self._client.post(self.url, headers=self._headers(), json=body)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if notification:
            return None
        resp.raise_for_status()
        obj = self._parse_body(resp)
        if "error" in obj:
            raise RuntimeError(f"MCP {method} 오류: {obj['error']}")
        return obj.get("result", {})

    # ── 공개 API ────────────────────────────────────────────
    async def connect(self) -> None:
        async with self._lock:
            if self._tools is not None:
                return
            await self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "medai-harness", "version": "0.1"},
            })
            try:
                await self._rpc("notifications/initialized", {}, notification=True)
            except Exception:
                pass  # 일부 서버는 안 받아도 동작한다
            res = await self._rpc("tools/list", {})
            self._tools = res.get("tools", [])

    async def list_tools(self) -> list[dict]:
        await self.connect()
        return list(self._tools or [])

    async def call(self, name: str, arguments: dict) -> str:
        """tools/call → 텍스트로 합쳐 반환. 실패는 예외 대신 오류 문자열.

        검색 단계 루프 안에서 예외가 터지면 문항 전체가 날아간다.
        모델에게 '이 도구 호출은 실패했다'를 알려주고 계속 가게 한다.
        """
        try:
            await self.connect()
            res = await self._rpc("tools/call", {"name": name, "arguments": arguments})
            parts = []
            for c in (res or {}).get("content", []):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                else:
                    parts.append(json.dumps(c, ensure_ascii=False))
            sc = (res or {}).get("structuredContent")
            if sc and not parts:
                parts.append(json.dumps(sc, ensure_ascii=False))
            return "\n".join(parts) or "(빈 결과)"
        except Exception as e:
            return f"[tool_error] {name}: {e!r}"

    def openai_tools(self, allow: Optional[set] = None,
                     desc_limit: int = 8000) -> list[dict]:
        """MCP tool 정의 → OpenAI chat/completions 의 tools 파라미터 형식.

        ★ description 을 자르지 않는다.
          서버가 써준 description 이 곧 LLM 의 사용 설명서다. 우리가 따로 쓸 필요가
          없는 대신, **자르면 그만큼 오작동한다**.
          실측(data/probe/tools.json):
            rag_vector_query 2,165자  ← 예전 1024 컷이면 절반이 날아갔다
            openapi_law_get_article 1,216 / rag_sql_query 1,108 /
            openapi_mfds_get_drug_indication 1,034
          rag_vector_query 의 뒷부분에 collection 이름과 필터 사용법이 들어 있어서,
          자르면 모델이 collection_name 을 못 채워 호출이 통째로 실패한다.

        allow 로 intent 별 서브셋만 노출한다 (l2.py TOOLSETS).
        outputSchema / _meta 는 보내지 않는다 — 호출에 불필요하고 매우 크다
        (rag_sql_query 의 outputSchema 하나가 15,797자다).
        """
        out = []
        for t in self._tools or []:
            if allow is not None and t["name"] not in allow:
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": (t.get("description") or "")[:desc_limit],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return out

    def tools_cost(self, allow: Optional[set] = None) -> dict:
        """이 서브셋을 매 요청에 실으면 얼마인지 — 예산 판단용."""
        ts = self.openai_tools(allow)
        chars = sum(len(json.dumps(t, ensure_ascii=False)) for t in ts)
        return {"tools": len(ts), "chars": chars, "approx_tokens": int(chars * 0.3)}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
