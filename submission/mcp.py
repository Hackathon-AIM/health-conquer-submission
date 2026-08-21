"""Lunit MCP Streamable HTTP 클라이언트."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


class MCPError(RuntimeError):
    pass


def _parse_sse(raw: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            value = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    if not messages:
        raise MCPError("MCP SSE response contained no JSON-RPC message")
    return messages[-1]


class LunitMCPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._next_id = 1
        self._lock = threading.Lock()
        self._legacy_session = ""
        self._legacy = False
        self._tools_cache: list[dict[str, Any]] | None = None

    def list_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is not None:
            return list(self._tools_cache)
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools_cache = list(tools)
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._rpc_once(method, params)
        except MCPError:
            if self._legacy or method != "tools/list":
                raise
            self._initialize_legacy()
            return self._rpc_once(method, params)

    def _rpc_once(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        actual_params = dict(params)
        if not self._legacy:
            actual_params.setdefault("_meta", {})["io.modelcontextprotocol/clientInfo"] = {
                "name": "aim-lunit-submission",
                "version": "0.2.0",
            }
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": actual_params}
        response, _headers = self._post(payload, method, params.get("name"))
        if response.get("error"):
            raise MCPError(f"MCP {method} error: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"MCP {method} returned no object result")
        return result

    def _initialize_legacy(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "aim-lunit-submission", "version": "0.2.0"},
            },
        }
        response, headers = self._post(payload, "initialize", None, legacy=True)
        if response.get("error") or not isinstance(response.get("result"), dict):
            raise MCPError(f"MCP initialize failed: {response.get('error') or response}")
        self._legacy = True
        self._legacy_session = headers.get("Mcp-Session-Id", "")
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            "notifications/initialized",
            None,
            allow_empty=True,
        )

    def _post(
        self,
        payload: dict[str, Any],
        method: str,
        name: str | None,
        *,
        legacy: bool = False,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.fm_api_key}",
        }
        if legacy or self._legacy:
            headers["MCP-Protocol-Version"] = "2025-11-25"
            if self._legacy_session:
                headers["Mcp-Session-Id"] = self._legacy_session
        else:
            headers["MCP-Protocol-Version"] = self.settings.mcp_protocol_version
            headers["Mcp-Method"] = method
            if name:
                headers["Mcp-Name"] = name
        req = urllib.request.Request(
            self.settings.mcp_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.mcp_timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
                response_headers = response.headers
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:700]
            raise MCPError(f"MCP HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MCPError(f"MCP request failed: {exc}") from exc
        if not raw.strip() and allow_empty:
            return {}, response_headers
        try:
            value = _parse_sse(raw) if "text/event-stream" in content_type else json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"MCP returned invalid JSON: {raw[:300]}") from exc
        if not isinstance(value, dict):
            raise MCPError("MCP returned non-object JSON-RPC response")
        return value, response_headers


def openai_tools(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in definitions:
        result.append({
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return result
