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
