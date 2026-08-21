"""토큰 텔레메트리 — 서버가 매 응답에 주는 usage 를 버리지 않는다.

이 파일이 있는 이유
  이 저장소의 예산 상수들은 전부 실측에서 나왔다. MAX_TOKENS=6144 도, thinking
  비중 46~50% 도, 잘림 1/12 도 그렇다. 좋은 숫자다. 그런데 그 측정은 **12문항짜리
  오프라인 프로브**였고, 실제 평가가 도는 동안에는 아무도 재지 않는다.
  call_fm 은 r.json() 을 그대로 돌려주고, 그 안의 `usage` 는 읽히지 않은 채 버려진다.
  로그에 남는 것은 `응답 %d자 / %.1fs` — 문자와 시간뿐이다.

  그래서 200문항 회차가 끝나도 이런 것들을 알 수 없다.
      - 이번 회차에서 몇 건이 max_tokens 에 걸려 잘렸는가
      - thinking 이 실제로 예산의 몇 %를 먹었는가 (문항마다 다르다)
      - 빈 content 가 몇 건이었는가  ← 확정 0점이라 가장 중요한 숫자다
      - 프롬프트가 실제로 몇 토큰이었는가 (compress 의 문자 예산이 맞았는가)

  전부 서버가 이미 알려주고 있는 것들이다. 주워 담기만 하면 된다.

무엇을 하지 않는가
  **동작을 바꾸지 않는다.** 이 모듈은 읽고 세기만 한다. 예산을 조이거나 호출을
  막지 않는다. 측정과 판단을 같은 커밋에 섞으면, 점수가 움직였을 때 어느 쪽
  때문인지 영영 알 수 없다.

  그리고 절대 예외를 밖으로 내지 않는다. 텔레메트리 때문에 답이 사라지면
  그건 순손실이다 — 빈 답은 확정 0점이고, 관측은 그만한 값이 없다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("driver.telemetry")

# 0 이면 집계만 하고 호출마다 줄을 남기지 않는다. 동시 30건에서 로그가 시끄러우면 끈다.
TELEMETRY_PER_CALL = os.environ.get("TELEMETRY_PER_CALL", "1") == "1"


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class StageStats:
    """한 단계(draft/review/classify/...)의 누적 관측."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    truncated: int = 0          # finish_reason == "length" — 답이 문장 중간에서 끊긴다
    empty: int = 0              # content 가 빈 응답 — 확정 0점
    seconds: float = 0.0
    max_prompt_tokens: int = 0
    max_completion_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        thinking_share = (
            self.reasoning_tokens / self.completion_tokens
            if self.completion_tokens else 0.0
        )
        return {
            "calls": self.calls,
            "prompt_tokens_total": self.prompt_tokens,
            "completion_tokens_total": self.completion_tokens,
            "prompt_tokens_avg": round(self.prompt_tokens / self.calls) if self.calls else 0,
            "completion_tokens_avg": round(self.completion_tokens / self.calls) if self.calls else 0,
            "prompt_tokens_max": self.max_prompt_tokens,
            "completion_tokens_max": self.max_completion_tokens,
            "thinking_share": round(thinking_share, 3),
            "truncated": self.truncated,
            "empty": self.empty,
            "seconds_avg": round(self.seconds / self.calls, 2) if self.calls else 0.0,
        }


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, StageStats] = {}

    def record(
        self,
        stage: str,
        data: dict[str, Any],
        *,
        requested_max_tokens: int = 0,
        seconds: float = 0.0,
    ) -> None:
        """FM 응답 하나를 관측한다. 무슨 일이 있어도 예외를 내지 않는다."""
        try:
            self._record(stage or "fm", data, requested_max_tokens, seconds)
        except Exception:  # noqa: BLE001
            # 관측이 답을 죽이는 일은 없어야 한다.
            log.debug("텔레메트리 기록 실패", exc_info=True)

    def _record(
        self,
        stage: str,
        data: dict[str, Any],
        requested_max_tokens: int,
        seconds: float,
    ) -> None:
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt = int(_f(usage.get("prompt_tokens")))
        completion = int(_f(usage.get("completion_tokens")))

        # vLLM 은 사고 토큰을 completion_tokens_details.reasoning_tokens 로 준다.
        # 없으면 reasoning 필드 길이로 대신 세지 않는다 — 문자는 토큰이 아니고,
        # 추정으로 채운 값이 실측처럼 보이면 그게 더 나쁘다.
        details = usage.get("completion_tokens_details")
        reasoning = int(_f((details or {}).get("reasoning_tokens"))) if isinstance(details, dict) else 0

        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = (message.get("content") or "") if isinstance(message, dict) else ""
        finish = str(choice.get("finish_reason") or "")

        with self._lock:
            stats = self._stages.setdefault(stage, StageStats())
            stats.calls += 1
            stats.prompt_tokens += prompt
            stats.completion_tokens += completion
            stats.reasoning_tokens += reasoning
            stats.seconds += seconds
            stats.max_prompt_tokens = max(stats.max_prompt_tokens, prompt)
            stats.max_completion_tokens = max(stats.max_completion_tokens, completion)
            if finish == "length":
                stats.truncated += 1
            if not str(content).strip():
                stats.empty += 1

        if not TELEMETRY_PER_CALL:
            return
        line = {
            "stage": stage,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "max_tokens": requested_max_tokens,
            "finish": finish,
            "content_chars": len(str(content)),
            "elapsed_s": round(seconds, 2),
        }
        if reasoning:
            line["reasoning_tokens"] = reasoning
            if completion:
                line["thinking_share"] = round(reasoning / completion, 3)
        # 예산을 얼마나 태웠는지가 한눈에 보여야 튜닝에 쓸 수 있다.
        if requested_max_tokens and completion:
            line["output_used"] = round(completion / requested_max_tokens, 3)
        if finish == "length" or not str(content).strip():
            # 이 둘은 점수에 직접 닿는다. 다른 줄과 같은 레벨로 묻히면 안 된다.
            log.warning("TOKENS %s", json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        else:
            log.info("TOKENS %s", json.dumps(line, ensure_ascii=False, separators=(",", ":")))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stages = {name: stats.as_dict() for name, stats in self._stages.items()}
        total = {
            "calls": sum(s["calls"] for s in stages.values()),
            "truncated": sum(s["truncated"] for s in stages.values()),
            "empty": sum(s["empty"] for s in stages.values()),
            "prompt_tokens_total": sum(s["prompt_tokens_total"] for s in stages.values()),
            "completion_tokens_total": sum(s["completion_tokens_total"] for s in stages.values()),
        }
        return {"total": total, "by_stage": stages}

    def reset(self) -> None:
        with self._lock:
            self._stages.clear()


TELEMETRY = Telemetry()


def staged(call, stage: str):
    """call_fm 을 stage 라벨에 묶어서 돌려준다.

    _timed 와 같은 수법이다 — 하위 모듈(router.classify, review, digest...)은
    call_fm 의 시그니처를 모르고 자기가 어느 단계인지도 모른다. 여기서 감싸면
    그 모듈들을 하나도 안 고치고 단계별로 나눠 볼 수 있다.
    """

    async def _call(messages, max_tokens, extra=None, **kw):
        kw.setdefault("stage", stage)
        return await call(messages, max_tokens, extra, **kw)

    return _call
