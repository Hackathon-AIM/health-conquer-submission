"""L2 · 라우팅 — 어느 소스를 부를지 결정.

라우터는 별도 컴포넌트가 아니다. L2가 intent를 정하고, 그 결과로 코드가 계산한다.
판단은 LLM, 매핑은 코드.

왜 LLM에게 sources를 직접 시키지 않는가
  · 오타(`guidelin`) → KeyError. 표 조회는 불가능
  · 같은 질문에 매번 다른 소스가 나올 수 있음 → 재현성 상실
  · 테스트 불가 → assert route(...) == {...} 를 못 씀
  · "약물 질문에 가이드라인도 보자" 를 반영하려면 프롬프트를 고치고 전량 재검증해야 함
    (표는 한 줄 고치면 전 케이스 즉시 반영)

규칙이 기본, LLM은 예외만.
"""

from __future__ import annotations

from .config import Config
from .contracts import Intent, QueryPlan

AVAILABLE: set[str] = {"guideline", "drug", "law", "hira", "pubmed"}

# 1:1이 아니라 1:N. drug_recommend는 약 정보(용법·주의)와
# 가이드라인(비약물 대처·내원 기준) 둘 다 필요하다.
INTENT_TO_SOURCES: dict[Intent, set[str]] = {
    Intent.EMERGENCY:       set(),
    Intent.DRUG_SAFETY:     {"drug"},
    Intent.DRUG_RECOMMEND:  {"drug", "guideline"},
    Intent.SYMPTOM_CONSULT: {"guideline"},
    Intent.INFO_REQUEST:    {"guideline"},
    Intent.POLICY:          {"law", "hira"},
}


def route(plan: QueryPlan, cfg: Config) -> list[str]:
    rc = cfg["routing"]

    # 응급은 설정으로 제어 (기본 [] = 검색 우회, A/B 대상)
    if plan.intent == Intent.EMERGENCY or plan.red_flag is not None:
        return list(cfg["emergency"].get("sources", []))

    sources = set(INTENT_TO_SOURCES.get(plan.intent, {"guideline"}))

    # ① 엔티티 기반 보정 — 의도와 무관하게 항상 참인 규칙
    #    "고혈압이 뭐예요"(guideline) vs "혈압약 부작용이 뭐죠"(drug)
    #    둘 다 info_request라 표만으로는 구분이 안 된다.
    if plan.entities.drugs:
        sources.add("drug")

    # ② LLM이 제안한 예외 — 의도로도 엔티티로도 안 잡히는 것(주로 pubmed)
    #    "고혈압 최신 연구는?" 같은 케이스
    for s in plan.extra_sources or []:
        if s == "pubmed" and not rc.get("allow_pubmed", True):
            continue
        sources.add(s)

    # ③ 방어 — 없는 소스 차단 + 개수 상한(불안하면 다 넣는 경향 억제)
    sources &= AVAILABLE
    if not sources:
        sources = {rc.get("fallback_source", "guideline")}

    ordered = [s for s in ("guideline", "drug", "law", "hira", "pubmed") if s in sources]
    return ordered[: int(rc.get("max_sources", 3))]
