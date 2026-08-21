import json
from collections.abc import Mapping
from typing import Any

import httpx

from medibot.config.settings import Settings


class MCPClientError(RuntimeError):
    """Raised when an MCP transport call fails or returns invalid JSON-RPC."""


class LunitMCPClient:
    """Minimal Streamable HTTP MCP client for Lunit retrieval tools."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._request_id = 0

    async def list_tools(self) -> list[dict[str, Any]]:
        data = await self._post("tools/list", {"cursor": None})
        result = self._result(data)
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._post(
            "tools/call",
            {
                "name": name,
                "arguments": dict(arguments),
            },
            mcp_name=name,
        )
        return self._result(data)

    async def _post(
        self,
        method: str,
        params: dict[str, Any],
        mcp_name: str | None = None,
    ) -> dict[str, Any]:
        self._request_id += 1
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.settings.mcp_protocol_version,
            "Mcp-Method": method,
        }
        if mcp_name:
            headers["Mcp-Name"] = mcp_name
        if self.settings.model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.model_api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": self.settings.mcp_protocol_version,
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "medibot",
                        "version": "0.1.0",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        if self._http_client is not None:
            response = await self._http_client.post(
                self.settings.mcp_url, json=payload, headers=headers
            )
            response.raise_for_status()
            return self._decode_response(response)

        async with httpx.AsyncClient(timeout=self.settings.source_timeout_s) as client:
            response = await client.post(
                self.settings.mcp_url, json=payload, headers=headers
            )
            response.raise_for_status()
            return self._decode_response(response)

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self.parse_sse_response(response.text)
        return response.json()

    def parse_sse_response(self, text: str) -> dict[str, Any]:
        final_data: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if not payload or payload == "[DONE]":
                continue
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                final_data = parsed
        if final_data is None:
            raise MCPClientError("SSE response did not contain a JSON-RPC result.")
        return final_data

    def _result(self, data: dict[str, Any]) -> dict[str, Any]:
        if "error" in data:
            raise MCPClientError(str(data["error"]))
        result = data.get("result")
        if not isinstance(result, dict):
            raise MCPClientError("MCP response result must be an object.")
        return result
