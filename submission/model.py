"""Lunit Model API의 OpenAI 호환 클라이언트."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings


class ModelError(RuntimeError):
    pass


@dataclass(slots=True)
class ChatResponse:
    message: dict[str, Any]
    raw: dict[str, Any]


class LunitModelClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def endpoint(self) -> str:
        return f"{self.settings.fm_api_url.rstrip('/')}/v1/chat/completions"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.1,
    ) -> ChatResponse:
        if not self.settings.fm_api_key:
            raise ModelError("LUNIT_FM_API_KEY is not set")
        payload: dict[str, Any] = {
            "model": self.settings.fm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(self.settings.max_tokens, 2048),
            "chat_template_kwargs": {"enable_thinking": self.settings.enable_thinking},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        data = self._post(payload)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("unexpected Model API response") from exc
        if not isinstance(message, dict):
            raise ModelError("Model API message is not an object")
        return ChatResponse(message=message, raw=data)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.fm_api_key}",
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.settings.model_timeout_s) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ModelError("Model API returned non-object JSON")
                return parsed
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                    raise ModelError(f"Model API HTTP {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.8 * (2**attempt)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 3:
                    break
                delay = 0.8 * (2**attempt)
            time.sleep(delay + random.random() * 0.15)
        raise ModelError(f"Model API request failed: {last_error}")
