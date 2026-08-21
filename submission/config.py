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


# 2026-08-22 실측 (scripts/probe_endpoint.py). 추측이 아니라 서버가 알려준 값이다.
#   /v1/models -> max_model_len = 131072
#   prompt_tokens 131,016 -> 200 / 131,073 -> 400 "maximum context length is 131072 tokens"
#   max_tokens 32,769 -> 400 "max_tokens exceeds 32768"
SERVER_MAX_CONTEXT = 131_072
SERVER_MAX_TOKENS = 32_768


# 평가 환경이 키를 주입해 주는지 확인되지 않았다. 주입되지 않으면 모든 생성이 실패하고
# 그 채점은 0점이므로 키를 코드에 들고 간다. 환경변수가 있으면 언제나 그쪽이 이긴다.
#
# ⚠️ 이 키들은 저장소 히스토리에 남는다. 대회가 끝나면 대시보드 /api-keys 에서 폐기할 것.
# 2026-08-22 실측: 둘 다 유효 (HTTP 200).
_TEAM_KEY = "lunit_ON-vm47lkCrk0U0iYflnVFMZBW9nEUTGkzlGsg7lshE"
_TEAM_KEY_PREVIOUS = "lunit_dFthkHMh2_gB2aVIo_mi5jznWpHoXbU2a2Od4hlVtf4"


def resolve_api_key() -> str:
    """환경변수 → 현재 팀 키 → 이전 팀 키. app.py 와 제출 드라이버가 같은 키를 쓰게 한다."""
    return os.environ.get("LUNIT_FM_API_KEY", "").strip() or _TEAM_KEY or _TEAM_KEY_PREVIOUS


@dataclass(slots=True, frozen=True)
class Settings:
    fm_api_url: str = os.environ.get("LUNIT_FM_API_URL", "https://model.hackathon.lunit.io")
    fm_api_key: str = resolve_api_key()
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

    # 입력 컨텍스트 작업 예산. 이 값 하나가 시스템 프롬프트·대화 길이·근거 블록·툴 스키마를
    # 전부 결정한다.
    #
    # 하드 실링은 SERVER_MAX_CONTEXT(131,072) 지만 기본값은 일부러 그보다 훨씬 작다.
    # 창에 들어간다와 모델이 그걸 제대로 쓴다는 다른 문제다 — 입력이 길어질수록
    # 성능이 불안정해지고(context rot), 5B 활성 모델에서 특히 그렇다. 게다가
    # 8~15초 지연 목표에서 12만 토큰 프롬프트는 그 자체로 예산 초과다.
    # 늘리려면 이 값만 올리면 된다. 나머지는 전부 역산된다.
    #
    # 이전의 LUNIT_MAX_CONTEXT_CHARS(문자 기준)를 대체한다. 문자 상한은 언어에 따라
    # 3배 넘게 어긋난다 — 실측으로 한국어는 1.19 자/토큰, 영문은 약 4 자/토큰이다.
    max_input_tokens: int = _int("LUNIT_MAX_INPUT_TOKENS", 32_768)
    # 21개 전부 보여 주면 스키마만으로 프롬프트가 흐려진다. 몇 개까지 보일지.
    max_mcp_tools: int = _int("LUNIT_MAX_MCP_TOOLS", 6)
    # 생성 단계에 넣을 근거 조각 수. context rot 때문에 넉넉히 넣는 게 곧 이득은 아니다.
    max_evidence_blocks: int = _int("LUNIT_MAX_EVIDENCE_BLOCKS", 5)
    # 서버 실측 상한은 32,768 (그 위는 output_limit_exceeded). 코드에 박혀 있던 2048 은
    # 사실이 아니었다 — 8,192 출력이 정상 완주하는 것을 확인했다.
    max_tokens: int = min(_int("FM_MAX_TOKENS", 2048), SERVER_MAX_TOKENS)
    enable_thinking: bool = os.environ.get("FM_THINKING", "0") == "1"


SETTINGS = Settings()
