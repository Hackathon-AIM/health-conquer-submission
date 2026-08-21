"""소형 입력 컨텍스트를 위한 토큰 회계와 예산 배분.

이 파일이 있는 이유
  L2 엔드포인트의 입력 컨텍스트가 우리가 가정했던 32K 가 아니다. 현재 코드는
  tool 결과를 4,000자, 근거 블록을 12,000자까지 실어 보낸다. 2~3K 토큰 창에서
  그건 요청이 통째로 거절되거나 앞부분이 조용히 잘려 나간다는 뜻이고, 잘리는 쪽은
  거의 항상 시스템 지침이다 (앞에 있으니까).

설계 원칙
  1. 모든 축소는 예산에서 역산한다. `4000` 같은 상수는 창 크기가 바뀌는 순간 거짓말이 된다.
  2. 추정치는 실측(usage.prompt_tokens)으로 보정한다. 한국어는 토크나이저마다
     char/token 이 1.0~2.0 으로 흔들려서 고정 계수 하나로는 못 맞춘다.
  3. 넘칠 때 무엇을 먼저 버릴지 코드로 고정한다: 오래된 대화 > 근거 꼬리 > 툴 스키마 > 시스템 지침.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

# 서버 실측: max_model_len 131,072, max_tokens 상한 32,768.
# 둘은 같은 창을 나눠 쓰므로, 최대 출력을 요구할 수 있는 여지를 남긴 것이 입력의 천장이다.
_SERVER_MAX_CONTEXT = 131_072
_SERVER_MAX_TOKENS = 32_768
_SERVER_INPUT_CEILING = _SERVER_MAX_CONTEXT - 2_048  # 기본 출력 예산만큼은 항상 비워 둔다

# 메시지 하나를 감싸는 chat template 오버헤드(role 태그·구분자). 모델마다 3~8 사이다.
_MESSAGE_OVERHEAD = 5
# tools 배열 자체를 감싸는 고정 비용.
_TOOLS_OVERHEAD = 12

# 문자 종류별 '문자당 토큰' 가중치. 보수적으로(=크게) 잡는다. 과대추정은 답이 조금
# 짧아질 뿐이지만 과소추정은 400 이나 조용한 잘림이 된다. 둘의 비용이 다르므로
# 대칭으로 두지 않는다.
# 2026-08-22 실측(scripts/probe_endpoint.py): 한국어 1,400자 -> 1,176 tok = 1.19 자/토큰.
# 즉 0.84 tok/자. 과소추정은 조용한 잘림이 되므로 그보다 살짝 위로 잡는다.
_W_HANGUL = 0.86      # 가-힣
_W_CJK = 1.00         # 한자·가나 — 거의 1자 1토큰
_W_ASCII_WORD = 0.28  # 영문·숫자 — 약 3.6자/토큰
_W_OTHER = 0.45       # 공백·문장부호·기타


def _char_weight(ch: str) -> float:
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return _W_HANGUL
    if 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xF900 <= code <= 0xFAFF:
        return _W_CJK
    if ch.isascii() and (ch.isalnum() or ch == "_"):
        return _W_ASCII_WORD
    return _W_OTHER


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


class TokenCounter:
    """토크나이저 없이 세고, 서버가 알려준 실제 값으로 스스로 보정한다.

    제출 컨테이너에는 tiktoken 도 transformers 도 없다 (빌드 5분 제한). 대신 우리는
    매 응답의 usage.prompt_tokens 라는 정답지를 공짜로 받는다. 그걸로 배율 하나를
    EWMA 로 굴리면 몇 번의 호출 만에 실제 토크나이저에 수렴한다.
    """

    def __init__(self, *, safety: float | None = None) -> None:
        if safety is None:
            safety = _env_float("LUNIT_TOKEN_SAFETY", 1.12)
        self._safety = safety
        self._scale = 1.0
        self._samples = 0
        self._lock = threading.Lock()

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def samples(self) -> int:
        return self._samples

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0
        raw = sum(_char_weight(ch) for ch in text)
        return max(1, int(raw * self._scale * self._safety + 0.5))

    def estimate_message(self, message: dict[str, Any]) -> int:
        total = _MESSAGE_OVERHEAD
        content = message.get("content")
        if isinstance(content, str):
            total += self.estimate_text(content)
        elif content is not None:
            total += self.estimate_text(_dumps(content))
        for key in ("name", "tool_call_id"):
            value = message.get(key)
            if isinstance(value, str):
                total += self.estimate_text(value)
        if message.get("tool_calls"):
            total += self.estimate_text(_dumps(message["tool_calls"]))
        return total

    def estimate_messages(self, messages: Iterable[dict[str, Any]]) -> int:
        return sum(self.estimate_message(message) for message in messages)

    def estimate_tools(self, tools: list[dict[str, Any]] | None) -> int:
        if not tools:
            return 0
        return _TOOLS_OVERHEAD + self.estimate_text(_dumps(tools))

    def estimate_request(
        self,
        messages: Iterable[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return self.estimate_messages(messages) + self.estimate_tools(tools)

    def observe(self, estimated: int, actual: int) -> None:
        """서버가 돌려준 prompt_tokens 로 배율을 보정한다."""
        if estimated <= 0 or actual <= 0:
            return
        # safety 는 의도적인 여유분이므로 보정에서 되살려 준다. 안 그러면 보정이
        # 여유분을 계속 깎아 내려 결국 마진이 0 이 된다.
        observed = (actual / estimated) * self._safety
        with self._lock:
            weight = 0.5 if self._samples == 0 else 0.25
            self._scale = min(2.5, max(0.5, (1 - weight) * self._scale + weight * observed))
            self._samples += 1

    def truncate(self, text: str, max_tokens: int, *, marker: str = " …") -> str:
        """max_tokens 안에 들어가도록 자른다. 문자 기준이 아니라 토큰 기준이다."""
        if max_tokens <= 0:
            return ""
        if self.estimate_text(text) <= max_tokens:
            return text
        target = max(1, max_tokens - self.estimate_text(marker) if marker else max_tokens)
        # 접두사의 가중치 합은 단조 증가하므로 이분탐색으로 경계를 찾는다.
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.estimate_text(text[:mid]) <= target:
                low = mid
            else:
                high = mid - 1
        return text[:low].rstrip() + marker

    def truncate_middle(self, text: str, max_tokens: int) -> str:
        """질문처럼 앞뒤가 다 중요한 텍스트는 가운데를 들어낸다."""
        if max_tokens <= 0:
            return ""
        if self.estimate_text(text) <= max_tokens:
            return text
        gap = "\n…[중략]…\n"
        budget = max(2, max_tokens - self.estimate_text(gap))
        head_tokens = budget // 2
        tail_tokens = budget - head_tokens
        head = self.truncate(text, head_tokens, marker="")
        low, high = 0, len(text)
        while low < high:
            mid = (low + high) // 2
            if self.estimate_text(text[mid:]) <= tail_tokens:
                high = mid
            else:
                low = mid + 1
        return head + gap + text[low:].lstrip()


@dataclass(frozen=True)
class ContextBudget:
    """입력 토큰 하나를 여러 소비자에게 나눈다.

    비율은 총량에 따라 달라진다. 창이 클수록 근거에 더 쓰고, 작을수록 시스템 지침과
    마지막 사용자 질문을 지키는 데 쓴다 — 그 둘이 없으면 답 자체가 성립하지 않는다.
    """

    total: int
    system: int
    conversation: int
    evidence: int
    tools: int
    reserve: int

    @classmethod
    def from_env(cls, total: int | None = None) -> "ContextBudget":
        if total is None:
            total = _env_int("LUNIT_MAX_INPUT_TOKENS", 32_768)
        return cls.allocate(total)

    @classmethod
    def allocate(cls, total: int) -> "ContextBudget":
        # 서버 실측 상한 131,072 에서 최대 출력분을 뺀 값이 입력의 물리적 천장이다.
        # 이걸 넘겨 달라고 하면 서버가 400 을 던지므로 여기서 미리 깎는다.
        total = max(256, min(int(total), _SERVER_INPUT_CEILING))
        # 마지막 방어선. 우리 추정이 빗나가도 서버 한계를 안 넘도록 남겨 둔다.
        reserve = max(48, int(total * 0.06))
        usable = total - reserve
        # 최소값도 총량에 비례해야 한다. 고정 하한 120 을 그대로 두면 total 이 작을 때
        # system+conversation 만으로 총량을 넘어선다 (실제로 total=256 에서 288 이 나왔다).
        floor = min(120, max(24, usable // 4))
        system = min(max(floor, int(usable * 0.22)), 700)
        tools = min(max(0, int(usable * 0.18)), 900)
        conversation = max(floor, int(usable * 0.30))
        evidence = usable - system - tools - conversation
        floor = int(usable * 0.12)
        if evidence < floor:
            # 근거를 넣을 자리가 안 나오면 툴 스키마부터 깎는다. 툴을 못 부르는 것보다
            # 부른 결과를 못 싣는 쪽이 더 나쁘다.
            take = min(floor - evidence, tools)
            tools -= take
            evidence += take
        return cls(
            total=total,
            system=system,
            conversation=conversation,
            evidence=max(0, evidence),
            tools=tools,
            reserve=reserve,
        )

    def scaled(self, factor: float) -> "ContextBudget":
        return ContextBudget.allocate(int(self.total * factor))

    def describe(self) -> dict[str, int]:
        return {
            "total": self.total,
            "system": self.system,
            "conversation": self.conversation,
            "evidence": self.evidence,
            "tools": self.tools,
            "reserve": self.reserve,
        }


@dataclass
class _Block:
    """원자적으로 살거나 죽는 메시지 묶음.

    assistant(tool_calls) 와 그에 딸린 tool 응답을 따로 떼면 OpenAI 호환 서버가
    400 을 던진다. 그래서 자르기 단위를 메시지가 아니라 이 묶음으로 잡는다.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0


def _group_blocks(messages: list[dict[str, Any]], counter: TokenCounter) -> list[_Block]:
    blocks: list[_Block] = []
    for message in messages:
        tokens = counter.estimate_message(message)
        attach = (
            blocks
            and message.get("role") == "tool"
            and any(m.get("tool_calls") for m in blocks[-1].messages)
        )
        if attach:
            blocks[-1].messages.append(message)
            blocks[-1].tokens += tokens
            continue
        blocks.append(_Block([message], tokens))
    return blocks


def fit_messages(
    messages: list[dict[str, Any]],
    budget_tokens: int,
    counter: TokenCounter,
    *,
    elision_marker: str = "[앞선 대화 일부 생략]",
) -> tuple[list[dict[str, Any]], int]:
    """예산 안에 들어가도록 대화를 뒤에서부터 채운다. (fitted, 버린 블록 수) 반환.

    규칙
      - system 메시지는 항상 남는다 (필요하면 잘라서라도).
      - 마지막 사용자 질문은 항상 남는다. 이것마저 넘치면 가운데를 들어낸다.
      - 나머지는 최신 순으로 예산이 허락하는 만큼만.
    """
    if not messages:
        return [], 0

    systems = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    fitted_systems: list[dict[str, Any]] = []
    remaining = budget_tokens
    for message in systems:
        cost = counter.estimate_message(message)
        if cost <= remaining:
            fitted_systems.append(message)
            remaining -= cost
            continue
        content = message.get("content")
        room = max(0, remaining - _MESSAGE_OVERHEAD)
        if isinstance(content, str) and room > 0:
            shortened = dict(message)
            shortened["content"] = counter.truncate(content, room)
            fitted_systems.append(shortened)
            remaining = 0
        # 자리가 아예 없으면 이 system 메시지는 버린다. 호출자가 compact 프롬프트를
        # 쓰도록 예산을 짜 두었으므로 여기까지 오는 건 비정상 경로다.

    if not rest:
        return fitted_systems, 0

    blocks = _group_blocks(rest, counter)

    # 마지막 블록(대개 현재 사용자 질문)은 무조건 확보한다.
    tail = blocks[-1]
    if tail.tokens > remaining:
        shrunk = [dict(m) for m in tail.messages]
        share = max(1, (remaining - _MESSAGE_OVERHEAD * len(shrunk)) // max(1, len(shrunk)))
        for message in shrunk:
            if isinstance(message.get("content"), str):
                message["content"] = counter.truncate_middle(message["content"], share)
        tail = _Block(shrunk, counter.estimate_messages(shrunk))
    kept: list[_Block] = [tail]
    remaining -= tail.tokens

    marker_cost = counter.estimate_message({"role": "user", "content": elision_marker})
    for block in reversed(blocks[:-1]):
        if block.tokens > remaining:
            continue
        kept.append(block)
        remaining -= block.tokens

    kept.reverse()
    dropped = len(blocks) - len(kept)
    output = list(fitted_systems)
    if dropped and remaining >= marker_cost:
        output.append({"role": "user", "content": elision_marker})
    for block in kept:
        output.extend(block.messages)
    return output, dropped


COUNTER = TokenCounter()
BUDGET = ContextBudget.from_env()
