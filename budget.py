"""요청 하나에 걸린 시간 예산.

평가 하네스가 얼마나 기다려 주는지 모른다. 늦은 무응답은 느린 답보다 훨씬 나쁘므로,
남은 시간을 보며 검색을 스스로 줄이고 답을 쓸 시간은 반드시 남긴다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Deadline:
    total: float
    started: float

    @classmethod
    def start(cls, total: float) -> "Deadline":
        return cls(total=total, started=time.monotonic())

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed)

    def expired(self, reserve: float = 0.0) -> bool:
        """reserve 초를 남겨둬야 한다면, 그만큼 일찍 만료로 본다."""
        return self.remaining() <= reserve


# 상류 호출 하나에 최소한 이만큼은 줘야 의미가 있다. 이보다 짧게 주면
# 연결·전송만 하다 끝나서 호출 자체가 낭비가 된다.
MIN_CALL_S = 5.0


def call_cap(
    deadline: "Deadline | None",
    cap: float,
    reserve: float = 0.0,
    floor: float = MIN_CALL_S,
) -> float | None:
    """상류 호출 하나에 걸 timeout(초). deadline 이 없으면 None(=호출자 기본값).

    호출할지 말지는 이 함수가 정하지 않는다 — 그건 `deadline.expired(reserve)` 의
    몫이다. 여기서는 "부르기로 했다면 얼마나 기다릴 것인가"만 정한다.

    floor 를 두는 이유: 남은 시간이 1초라고 1초짜리 timeout 을 걸면 반드시 실패한다.
    부를 값어치가 없으면 애초에 expired() 에서 걸러야 하고, 부르기로 했다면
    최소한의 기회는 줘야 한다.
    """
    if deadline is None:
        return None
    return max(floor, min(cap, deadline.remaining() - reserve))


def answer_timeout(
    deadline: "Deadline | None", cap: float, floor: float
) -> float | None:
    """**최종 답변 호출**에 걸 timeout. 남은 시간으로 깎지 않는다.

    예산은 답을 쓸 시간을 지키려고 존재한다. 그런데 그 예산으로 답변 호출 자체를
    깎으면 앞 단계가 시간을 흘렸을 때 답변이 굶어 죽는다 — 실측에서 30건 중 6건이
    이렇게 빈 답이 됐고, 빈 답은 0점이다.

    그래서 여기서는 남은 시간이 아무리 없어도 floor 만큼은 준다. 늦은 답이
    없는 답보다 낫다는 판단이고, 그 판단은 이 프로젝트 곳곳에 이미 적혀 있다.
    """
    if deadline is None:
        return None
    return max(floor, min(cap, deadline.remaining()))
