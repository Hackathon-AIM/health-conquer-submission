"""L1 · 엔티티 추출 + 정규화 (결정론적, ~0ms)

담당: C

설계 원칙
  · 규칙이 확실히 이기는 것만 뽑는다 (약물 · 위험인자 · 시간)
  · 증상은 표현이 무한하므로 L2 LLM에 맡긴다 (안전 판정에 안 쓰이므로 위험 없음)
  · 사전은 창작하지 않는다 — 식약처 목록에서 자동 생성 (data/build_dicts.py)

왜 하는가
  자연어를 "기계가 조회할 수 있는 키(성분코드)"로 바꾸기 위함.
  DUR API는 "타이레놀같은거"를 못 먹는다. M040702를 먹는다.
  엔티티 추출이 없으면 조회를 못 하고, 조회를 못 하면 전부 추론이 되고,
  추론이면 5B가 프론티어를 못 이긴다.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .config import DATA
from .contracts import DrugMention, Entities, RiskFactor

# ─────────────────────────────────────────────────────────────
# 정규화
# ─────────────────────────────────────────────────────────────
_SUFFIX = re.compile(r"(정|캡슐|캅셀|시럽|산|주|액|이알|서방정|연질캡슐|정제|현탁액)$")


def key(s: str) -> str:
    """표기 흔들림 제거.

    주의: 제형 접미사는 자르되 제품을 구분하는 수식어("콜드", "이브")는 남겨야 한다.
    "타이레놀"과 "타이레놀콜드"를 합쳐버리면 용량 조언이 조용히 틀린다.
    """
    s = str(s).strip().lower().replace(" ", "").replace("·", "")
    return _SUFFIX.sub("", s)


# ─────────────────────────────────────────────────────────────
# 사전 로딩 (data/build_dicts.py 산출물)
# ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _dicts() -> tuple[dict, dict, dict]:
    def _load(name: str, default: dict) -> dict:
        p = Path(DATA) / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return default

    drug_map = _load("drug_map.json", {})            # key(이름) -> 성분코드
    prod2ing = _load("product_to_ingredients.json", {})  # key(제품) -> [성분코드]  ※복합제 전개
    drug_class = _load("drug_class.json", {})        # "해열진통제" -> [대표 성분]
    return drug_map, prod2ing, drug_class


def drug_map() -> dict:
    return _dicts()[0]


def product_to_ingredients() -> dict:
    return _dicts()[1]


def drug_class() -> dict:
    return _dicts()[2]


# ─────────────────────────────────────────────────────────────
# 위험인자 — 키워드가 유한해서 규칙이 잘 먹는다
# ─────────────────────────────────────────────────────────────
# ⚠️ 한국어 부분문자열 오탐에 주의할 것.
#    r"술" 은 '수술·기술·예술·시술'에 매칭된다.
#    실제로 "처치·수술 및 그 밖의 치료"(국민건강보험법 제41조)를 음주로 오인해
#    법률 답변에 음주 경고가 붙는 사고가 났다.
#    → (?<![가-힣]) 로 앞 글자가 한글이 아닐 때만 매칭한다.
#      (뒤는 조사가 붙으므로 lookahead를 걸면 안 된다: "술을", "술 먹고")
#    같은 이유로 '신장'(키), '젖'(젖다) 등도 문맥을 요구한다.
RISK_PATTERNS: dict[str, list[str]] = {
    "alcohol":   [r"(?<![가-힣])술(?![기의])", r"음주", r"소주", r"맥주", r"막걸리",
                  r"와인", r"위스키", r"폭탄주", r"회식", r"숙취", r"해장",
                  r"취했|취함|주량|과음"],
    "pregnancy": [r"임신", r"임산부", r"임부", r"태아", r"출산\s*후"],
    "lactation": [r"수유", r"모유", r"젖\s*(먹이|병)"],
    "smoking":   [r"담배", r"흡연", r"금연", r"전자담배"],
    "liver":     [r"간\s*(질환|기능|수치|손상|이\s*안\s*좋)", r"지방간", r"간염",
                  r"간경화", r"간독성"],
    "kidney":    [r"신장\s*(질환|기능|병|이|에|수치)", r"콩팥", r"투석",
                  r"사구체", r"신부전"],
    "driving":   [r"운전", r"기계\s*조작", r"작업\s*중"],
}
_RISK_COMPILED = {k: [re.compile(p) for p in v] for k, v in RISK_PATTERNS.items()}

_AGE = re.compile(r"(\d{1,3})\s*(살|세)")
_TEMPORAL = re.compile(
    r"(그저께|엊그제|어제|오늘|아침부터|밤부터|"
    r"\d+\s*(?:일|주|주일|개월|달|년)\s*(?:전|째|동안|간))"
)


def extract_risk_factors(text: str, age: int | None = None) -> list[RiskFactor]:
    out: list[RiskFactor] = []
    for rtype, pats in _RISK_COMPILED.items():
        for p in pats:
            m = p.search(text)
            if m:
                out.append(RiskFactor(type=rtype, detail=m.group(0)))  # type: ignore[arg-type]
                break

    m = _AGE.search(text)
    a = int(m.group(1)) if m else age
    if a is not None:
        if a >= 65:
            out.append(RiskFactor(type="elderly", detail=f"{a}세"))
        elif a < 12:
            out.append(RiskFactor(type="pediatric", detail=f"{a}세"))
    return out


def extract_temporal(text: str) -> str | None:
    m = _TEMPORAL.search(text)
    return m.group(0) if m else None


def extract_age(text: str) -> int | None:
    m = _AGE.search(text)
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────
# 약물 — 규칙 추출
# ─────────────────────────────────────────────────────────────
def rule_extract_drugs(text: str) -> set[str]:
    """사전에 있는 이름이 원문에 문자 그대로 있는지만 본다.

    recall은 낮지만(오타·구어체 못 잡음) 원문에 실재한다는 게 보장된다.
    → LLM 추출 결과를 검증할 때 앵커 역할.
    """
    t = key(text)
    dm = drug_map()
    return {name for name in dm if len(name) >= 2 and name in t}


def rule_extract_classes(text: str) -> set[str]:
    """'해열진통제', '아세트아미노펜 계열' 같은 효능군·성분군."""
    return {c for c in drug_class() if c in text}


# ─────────────────────────────────────────────────────────────
# ⭐ 규칙 ∪ LLM 합집합 → span 검증 → 사전 필터
# ─────────────────────────────────────────────────────────────
def merge_drug_candidates(
    text: str,
    rule_hits: set[str],
    llm_mentions: list[dict] | None = None,
) -> list[DrugMention]:
    """recall은 LLM에서, precision은 사전에서.

    1단계(후보 수집): 많이 모을수록 좋다. 쓰레기가 섞여도 된다.
    2단계(필터):
        ① span이 실제 원문에 있는가 → 환각 차단
        ② 사전에 실존하는 약인가   → 오독 차단
    """
    dm, p2i = drug_map(), product_to_ingredients()
    out: dict[str, DrugMention] = {}

    for name in rule_hits:
        k = key(name)
        out[k] = DrugMention(name=name, span=name, ingredient_codes=p2i.get(k, [dm[k]] if k in dm else []))

    for m in llm_mentions or []:
        name = str(m.get("name", "")).strip()
        span = str(m.get("span", "") or name).strip()
        if not name:
            continue
        # ① 환각 차단 — 근거 표현이 원문에 없으면 버린다
        if span and span not in text and key(span) not in key(text):
            continue
        k = key(name)
        # ② 실존 확인
        if k not in dm and k not in p2i:
            continue
        if k in out:
            continue
        out[k] = DrugMention(name=name, span=span, ingredient_codes=p2i.get(k, [dm[k]] if k in dm else []))

    return list(out.values())


def expand_class(term: str) -> list[str]:
    """모호하면 넓게 판정한다.

    "해열진통제"는 성분 확정이 안 되므로 그 효능군의 대표 성분 전부에 대해 검사.
    모호하게 말했다고 검사를 건너뛰면 안 된다 — 오히려 모호할수록 넓게.
    """
    return drug_class().get(term, [term])


def to_ingredient_codes(names: list[str]) -> set[str]:
    """제품명 → 성분코드 (복합제는 1:N 전개).

    ⚠️ 전개하지 않으면 '감기약 + 두통약'에서 아세트아미노펜 중복을 놓친다.
    """
    dm, p2i = drug_map(), product_to_ingredients()
    codes: set[str] = set()
    for n in names:
        for ing in expand_class(n):
            k = key(ing)
            if k in p2i:
                codes |= set(p2i[k])
            elif k in dm:
                codes.add(dm[k])
    return codes


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────
def extract(text: str, age: int | None = None) -> Entities:
    """L1 규칙 추출. 증상은 비워둔 채 L2에 넘긴다."""
    rule_hits = rule_extract_drugs(text) | rule_extract_classes(text)
    return Entities(
        drugs=merge_drug_candidates(text, rule_hits),
        symptoms=[],                       # ← L2 LLM 담당
        risk_factors=extract_risk_factors(text, age),
        temporal=extract_temporal(text),
    )
