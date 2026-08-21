"""환경변수 기반 제출 런타임 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(slots=True, frozen=True)
class Settings:
    fm_api_url: str = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io")
    fm_api_key: str = os.environ.get("LUNIT_FM_API_KEY", "")
    fm_model: str = os.environ.get("LUNIT_FM_MODEL", "Lunit/L2-preview")
    mcp_url: str = os.environ.get("LUNIT_MCP_URL", "https://mcp.hackathon.lunit.io/mcp")
    mcp_protocol_version: str = os.environ.get("LUNIT_MCP_PROTOCOL_VERSION", "2026-07-28")
    public_model_name: str = os.environ.get("HARNESS_MODEL_NAME", "conquer-health-l2-native")

    model_timeout_s: float = _float("FM_TIMEOUT", 180.0)
    mcp_timeout_s: float = _float("LUNIT_MCP_TIMEOUT_S", 30.0)
    retrieval_timeout_s: float = _float("LUNIT_RETRIEVAL_TIMEOUT_S", 90.0)
    max_retrieval_steps: int = _int("LUNIT_MAX_RETRIEVAL_STEPS", 6)
    max_tool_calls: int = _int("LUNIT_MAX_TOOL_CALLS", 4)
    max_retrieval_calls: int = _int("LUNIT_MAX_RETRIEVAL_CALLS", 1)
    max_context_chars: int = _int("LUNIT_MAX_CONTEXT_CHARS", 12_000)
    max_tokens: int = min(_int("FM_MAX_TOKENS", 2048), 2048)
    enable_thinking: bool = os.environ.get("FM_THINKING", "0") == "1"


SETTINGS = Settings()
