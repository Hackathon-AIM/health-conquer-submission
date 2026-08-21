"""L4b · risk_check — DUR 8종이 못 잡는 조합.

담당: C

왜 필요한가
  DUR 8종은 전부 약-약 또는 약-환자속성(나이·임신)이다.
  약-알코올, 약-음식, 약-장기기능은 아예 다루지 않는다.

  "어제 술 너무 먹었는데 머리가 아파요"
    → L1: 약물 없음 → L1b DUR 스킵
    → L4: "숙취 두통엔 아세트아미노펜 계열이…"  ← 검사 안 된 약 추천
    → 게다가 음주+아세트아미노펜은 DUR에 없어서 DUR을 돌렸어도 못 잡는다.

데이터를 창작하지 말 것
  의약품 허가사항의 '사용상의 주의사항' 필드에 서술형으로 다 있다.
  다만 표현이 제각각이라 정규식 파싱은 recall이 낮고,
  수만 건을 LLM으로 전처리하기엔 비용이 안 맞는다.

  → 현실적 접근(파레토 + 키워드 스크리닝):
     ① 키워드 스크리닝으로 위험 신호를 넓게 잡는다 (정밀하진 않지만 즉시 되고 recall 높음)
     ② 실제로 언급될 상위 50~100개 약만 정제한다
        (타이레놀·이부프로펜·아스피린·게보린·판피린·지르텍·겔포스 … 가 질문의 90%)

⚠️ 의학적 논쟁이 있는 항목은 단정하지 말 것
  음주 후 아세트아미노펜은 상용량·1회 음주와 상습 음주의 위험도가 다르다.
  이런 주제는 "음주 습관과 용량에 따라 다르며 확실치 않으면 피하는 편이 안전"처럼
  불확실성을 명시해야 HealthBench 가점이다. 단정하면 감점.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import DATA
from ..contracts import RiskFactor, SafetyHit
from ..entities import expand_class, key

# 허가사항 '사용상의 주의사항' 텍스트에서 위험 신호를 넓게 잡는 키워드.
# 정밀하진 않지만 "이 약은 음주 관련 주의사항이 있다"까지는 확실히 알 수 있다.
RISK_SIGNALS: dict[str, list[str]] = {
    "alcohol":   ["음주", "알코올", "술"],
    "pregnancy": ["임부", "임신", "태아"],
    "lactation": ["수유부", "모유"],
    "liver":     ["간장애", "간기능", "간독성", "간질환"],
    "kidney":    ["신장애", "신기능", "신독성", "신부전"],
    "elderly":   ["고령자", "노인"],
    "driving":   ["졸음", "운전", "기계조작", "주의력"],
    "pediatric": ["소아", "영아", "어린이"],
}

# 시드 규칙 — data/risk_rules.yaml 이 있으면 그쪽이 정본.
# 8/21 오전에 C가 허가사항 파싱으로 상위 50개를 채울 것.
SEED_RULES: dict[str, dict[str, str]] = {
    "아세트아미노펜": {
        "alcohol": ("음주, 특히 상습적인 음주 시 간에 부담이 될 수 있습니다. "
                    "위험도는 음주 습관과 복용량에 따라 다르므로, 확실치 않으면 "
                    "복용 전 약사와 상의하시는 편이 안전합니다."),
        "liver": "간 기능이 저하된 경우 대사가 지연될 수 있어 용량 조절이 필요합니다.",
    },
    "이부프로펜": {
        "alcohol": "음주와 함께 복용하면 위장관 출혈 위험이 증가할 수 있습니다.",
        "kidney": "신장 기능이 저하된 경우 신중히 투여해야 합니다.",
        "pregnancy": "임신부에게는 투여하지 않습니다.",
        "elderly": "고령자는 위장관 부작용 위험이 높아 신중히 투여합니다.",
    },
    "아스피린": {
        "alcohol": "음주와 함께 복용하면 위장관 출혈 위험이 증가할 수 있습니다.",
        "pediatric": "소아·청소년의 바이러스 감염 시에는 사용하지 않습니다.",
        "pregnancy": "임신 후기에는 사용하지 않습니다.",
    },
    "나프록센": {
        "alcohol": "음주와 함께 복용하면 위장관 출혈 위험이 증가할 수 있습니다.",
        "kidney": "신장 기능 저하 시 신중히 투여합니다.",
    },
    "클로르페니라민": {
        "alcohol": "음주 시 졸음·진정 작용이 강해질 수 있습니다.",
        "driving": "졸음이 올 수 있으므로 운전이나 기계 조작은 피하세요.",
        "elderly": "고령자는 항콜린 부작용에 민감할 수 있습니다.",
    },
}


@lru_cache(maxsize=1)
def _rules() -> dict[str, dict[str, str]]:
    rules = {key(k): v for k, v in SEED_RULES.items()}
    p = Path(DATA) / "risk_rules.yaml"
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k, v in loaded.items():
            rules[key(k)] = v
    # 허가사항 스크리닝 산출물이 있으면 병합(정본은 명시 규칙)
    j = Path(DATA) / "risk_screen.json"
    if j.exists():
        screened = json.loads(j.read_text(encoding="utf-8"))
        for k, types in screened.items():
            kk = key(k)
            rules.setdefault(kk, {})
            for t in types:
                rules[kk].setdefault(t, GENERIC[t])
    return rules


GENERIC: dict[str, str] = {
    "alcohol": "이 약은 음주와 관련된 주의사항이 있습니다. 복용 전 약사와 상의하세요.",
    "pregnancy": "임신 중에는 복용 전 반드시 의사와 상의하세요.",
    "lactation": "수유 중에는 복용 전 반드시 의사와 상의하세요.",
    "liver": "간 기능이 저하된 경우 주의가 필요합니다.",
    "kidney": "신장 기능이 저하된 경우 주의가 필요합니다.",
    "elderly": "고령자는 신중히 투여해야 합니다.",
    "driving": "졸음이 올 수 있으므로 운전이나 기계 조작에 주의하세요.",
    "pediatric": "소아에게는 연령·체중에 따른 용량 확인이 필요합니다.",
}


def screen_caution_text(caution: str) -> set[str]:
    """허가사항 '사용상의 주의사항' 원문 → 위험인자 타입 집합.

    B가 의약품 엔드포인트에서 받아온 텍스트를 여기에 넣어
    data/risk_screen.json 을 만든다.
    """
    return {t for t, kws in RISK_SIGNALS.items() if any(kw in caution for kw in kws)}


def check(drug_names: list[str], risk_factors: list[RiskFactor]) -> list[SafetyHit]:
    """약물 × 위험인자 교차 검사.

    모호하게 말했을수록 넓게 판정한다.
    "해열진통제를 드셔보세요" + 음주
      → 아세트아미노펜(간), 이부프로펜(위장), 아스피린(위장) 셋 다 걸림
      → "아세트아미노펜 계열은 간에, 소염진통제 계열은 위장에 부담"으로 답할 수 있다.
    검사를 건너뛰는 것보다 넓게 잡는 게 안전하고, 완결성 축에서도 유리하다.
    """
    if not drug_names or not risk_factors:
        return []

    rules = _rules()
    types = [r.type for r in risk_factors]
    out: list[SafetyHit] = []
    seen: set[tuple[str, str]] = set()

    for name in drug_names:
        for ing in expand_class(name):          # 효능군이면 대표 성분들로 확장
            k = key(ing)
            rule = rules.get(k)
            if not rule:
                continue
            for t in types:
                if t not in rule:
                    continue
                if (k, t) in seen:
                    continue
                seen.add((k, t))
                out.append(SafetyHit(
                    kind="위험인자",
                    severity=70,
                    drugs=[ing],
                    risk_factor=t,
                    reason=rule[t],
                    source="output",
                ))
    return out
