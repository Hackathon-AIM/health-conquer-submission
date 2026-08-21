"""계층 간 계약(contract).

⚠️ Python 3.9 호환
  pydantic은 런타임에 어노테이션을 평가하므로 `X | None`(PEP 604, 3.10+)을 쓰면
  macOS 기본 파이썬(3.9)에서 TypeError가 난다. 여기서는 Optional[...] 만 쓴다.
  (다른 모듈은 `from __future__ import annotations` 덕에 문자열로 남아 문제없다.)

이 파일이 팀 4명이 병렬로 일할 수 있게 하는 유일한 근거다.
여기 정의된 스키마만 지키면 A/B/C/D가 서로를 기다리지 않는다.

⚠️ 필드 순서 주의
LLM은 왼쪽→오른쪽으로 생성하므로 JSON 필드 순서가 곧 사고 순서다.
"이유"를 반드시 "결론"보다 먼저 두어야 사후 합리화가 아닌 실제 판단이 된다.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# L2 · 의도
# ─────────────────────────────────────────────────────────────
class Intent(str, Enum):
    EMERGENCY = "emergency"              # 응급 — 검색 우회
    DRUG_SAFETY = "drug_safety"          # 이 약 먹어도 되나요
    DRUG_RECOMMEND = "drug_recommend"    # 뭘 먹어야 하나요
    SYMPTOM_CONSULT = "symptom_consult"  # 증상 상담
    INFO_REQUEST = "info_request"        # 정보가 궁금해요
    POLICY = "policy"                    # 보험 · 제도


SourceName = Literal["guideline", "drug", "law", "hira", "pubmed"]


# ─────────────────────────────────────────────────────────────
# L1 · 엔티티
# ─────────────────────────────────────────────────────────────
class DrugMention(BaseModel):
    """LLM 추출 시 span을 함께 요구한다 → 원문에 없으면 환각으로 간주하고 버린다."""

    name: str
    span: str = ""                       # 원문에서 근거가 된 부분
    ingredient_codes: list[str] = Field(default_factory=list)


class RiskFactor(BaseModel):
    type: Literal[
        "alcohol", "pregnancy", "lactation", "smoking",
        "liver", "kidney", "elderly", "pediatric", "driving",
    ]
    when: Optional[str] = None              # "어제", "상시"
    detail: Optional[str] = None


class Entities(BaseModel):
    drugs: list[DrugMention] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    temporal: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# L1 · 레드플래그
# ─────────────────────────────────────────────────────────────
class RedFlagHit(BaseModel):
    category: str                        # cardiac / neuro / respiratory / ...
    pattern: str
    matched: str


# ─────────────────────────────────────────────────────────────
# L2 · QueryPlan — 계층 간 유일한 인터페이스
# ─────────────────────────────────────────────────────────────
class QueryPlan(BaseModel):
    # ① 먼저 생각하게 하는 필드들 (순서가 중요하다)
    reasoning: str = ""                          # 왜 이 의도로 봤는가
    missing_info: list[str] = Field(default_factory=list)
    why_it_changes_answer: str = ""              # 그 정보가 없으면 답이 어떻게 달라지나

    # ② 그다음 결론
    intent: Intent = Intent.SYMPTOM_CONSULT
    need_followup: bool = False

    # ②-b 응답 설계 — 채점 축에 직접 대응하는 필드
    #
    # intent 는 "무엇을 검색할까"(라우팅)만 정한다. 루브릭이 실제로 채점하는 것은
    # "어떻게 답할까"다. 그래서 축마다 필드를 따로 둔다.
    #   depth               ← 응답 깊이 (단순 질문에 장문 = 감점, 복잡한데 얕음 = 미획득)
    #   clarifying_question ← 맥락 탐색 (되묻기). 멀티턴 티키타카의 실체
    #   uncertainty         ← 불확실성 하 응답 (무엇이 불확실한지 명시)
    #   persona             ← 상대 수준별 소통
    depth: Literal["brief", "standard", "thorough"] = "standard"
    clarifying_question: str = ""    # 실제로 물을 문장 1개. 없으면 빈 문자열
    uncertainty: str = ""            # 답변에서 불확실하다고 밝혀야 할 것

    # ③ 부가 정보
    persona: Literal["layperson", "clinician"] = "layperson"
    entities: Entities = Field(default_factory=Entities)
    extra_sources: list[str] = Field(default_factory=list)   # 규칙으로 안 잡히는 예외(주로 pubmed)
    queries: dict[str, str] = Field(default_factory=dict)    # 소스별 검색어
    expects_drug_output: bool = False                        # 답변에 약물이 등장할 것인가

    # ④ 코드가 채우는 필드 (LLM 출력 아님)
    sources: list[str] = Field(default_factory=list)
    red_flag: Optional[RedFlagHit] = None


# ─────────────────────────────────────────────────────────────
# L3 · 검색
# ─────────────────────────────────────────────────────────────
class Doc(BaseModel):
    source: str
    title: str = ""
    text: str
    url: Optional[str] = None
    locator: Optional[str] = None           # "제41조", "p.32", "PMID:12345"
    score: float = 0.0

    def cite(self) -> str:
        parts = [self.source]
        if self.title:
            parts.append(self.title)
        if self.locator:
            parts.append(self.locator)
        return "[" + "|".join(parts) + "]"


class Context(BaseModel):
    docs: list[Doc] = Field(default_factory=list)
    tokens_used: int = 0
    dropped: int = 0                     # 예산 초과로 버린 문서 수 (조용한 절삭 금지)

    def render(self) -> str:
        return "\n\n".join(f"{d.cite()} {d.text}" for d in self.docs)


# ─────────────────────────────────────────────────────────────
# L1b / L4b · 안전 게이트
# ─────────────────────────────────────────────────────────────
Severity = int   # 100=절대금기 … 30=경미


class SafetyHit(BaseModel):
    kind: Literal[
        "병용금기", "특정연령대금기", "임부금기", "용량주의",
        "투여기간주의", "노인주의", "효능군중복", "서방정분할주의",
        "위험인자",                       # risk_check — DUR 8종 밖
    ]
    severity: Severity = 50
    drugs: list[str] = Field(default_factory=list)
    risk_factor: Optional[str] = None
    reason: str = ""
    source: Literal["input", "output"] = "input"   # L1b인가 L4b인가

    @property
    def absolute(self) -> bool:
        return self.severity >= 90


# ─────────────────────────────────────────────────────────────
# L4c · 비평
# ─────────────────────────────────────────────────────────────
class Violation(BaseModel):
    item: str
    why: str = ""
    how: str = ""
    confidence: float = 1.0


class Critique(BaseModel):
    violations: list[Violation] = Field(default_factory=list)
    passed: bool = True


# ─────────────────────────────────────────────────────────────
# 세션 · 턴
# ─────────────────────────────────────────────────────────────
class SessionState(BaseModel):
    age: Optional[int] = None
    sex: Optional[Literal["M", "F"]] = None
    pregnant: Optional[bool] = None
    conditions: list[str] = Field(default_factory=list)
    medications: list[str] = Field(default_factory=list)      # 성분코드 (복용 중)
    medication_names: list[str] = Field(default_factory=list)  # 복용 중인 약 이름
    asked_about: list[str] = Field(default_factory=list)       # 문의만 한 약 (복용 아님)
    risk_factors: list[RiskFactor] = Field(default_factory=list)
    symptom_duration: Optional[str] = None
    prior_advice: list[str] = Field(default_factory=list)
    history: list[dict[str, str]] = Field(default_factory=list)

    def known_keys(self) -> List[str]:
        """이미 아는 것 — 불필요한 되묻기를 막기 위해 분류기에 넘긴다."""
        out = []
        if self.age is not None:
            out.append("age")
        if self.pregnant is not None:
            out.append("pregnancy")
        if self.medications:
            out.append("medications")
        if self.symptom_duration:
            out.append("symptom_duration")
        return out


class TurnResult(BaseModel):
    answer: str
    plan: QueryPlan
    context: Context = Field(default_factory=Context)
    safety_hits: list[SafetyHit] = Field(default_factory=list)
    critique: Optional[Critique] = None
    latency_ms: dict[str, int] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)
