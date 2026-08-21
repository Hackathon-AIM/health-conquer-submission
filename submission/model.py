"""Lunit Model API의 OpenAI 호환 클라이언트."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .context import COUNTER, ContextBudget, fit_messages

log = logging.getLogger("driver.model")


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

        # 마지막 방어선. 호출자가 이미 예산을 맞춰 보냈더라도, 창을 넘긴 요청이
        # 여기를 통과하는 일은 없어야 한다. 잘림은 조용하고 그 대가는 0점이다.
        messages, estimated = self._fit(messages, tools)

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
        self._calibrate(estimated, data)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("unexpected Model API response") from exc
        if not isinstance(message, dict):
            raise ModelError("Model API message is not an object")
        return ChatResponse(message=message, raw=data)

    def _fit(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        budget = ContextBudget.allocate(self.settings.max_input_tokens)
        tool_tokens = COUNTER.estimate_tools(tools)
        room = budget.total - budget.reserve - tool_tokens
        estimated = COUNTER.estimate_request(messages, tools)
        if estimated <= budget.total - budget.reserve:
            return messages, estimated
        fitted, dropped = fit_messages(messages, max(64, room), COUNTER)
        estimated = COUNTER.estimate_request(fitted, tools)
        log.warning(
            "입력 예산 초과 — 마지막 방어선에서 %d블록 잘라냄 (%d→%d tok / 한도 %d, 툴 %d)",
            dropped, COUNTER.estimate_request(messages, tools), estimated, budget.total, tool_tokens,
        )
        return fitted, estimated

    @staticmethod
    def _calibrate(estimated: int, data: dict[str, Any]) -> None:
        """서버가 알려준 실제 prompt_tokens 로 추정기를 보정한다.

        토크나이저를 못 들고 가는 대신, 매 호출마다 정답지를 한 장씩 받는 셈이다.
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        actual = usage.get("prompt_tokens")
        if isinstance(actual, int) and actual > 0:
            COUNTER.observe(estimated, actual)

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
