"""L1b / L4b · DUR 관문 (결정론적).

담당: C

DUR = Drug Utilization Review, 의약품 안전사용 서비스.
병원·약국 전산에서 처방 입력 시점에 충돌을 검사해 경고를 띄우는 시스템.
심평원이 운영하고 식약처가 금기 목록을 고시한다.

핵심은 '판단'이 아니라 '조회'라는 점
              검색(RAG)              DUR
  입력        자연어 질문            성분코드
  출력        관련 있어 보이는 top-k  금기 여부(있다/없다)
  성격        확률적                 결정론적
  틀리는가    가능                   불가능

⚠️ 자주 하는 오해: "약 2개 이상이면 조회" — 틀렸다.
   8종은 각각 트리거가 다르며, 약이 하나만 있어도 5종이 걸린다.
   "타이레놀 하루 몇 알?"은 약이 하나지만 용량주의가 걸려야 한다.

⚠️ DUR이 못 잡는 것: 약-알코올, 약-음식, 약-장기기능.
   8종은 전부 약-약 또는 약-환자속성(나이·임신)이다.
   → gates/risk.py 가 그 공백을 메운다.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from functools import lru_cache

from ..contracts import RiskFactor, SafetyHit, SessionState
from ..entities import expand_class, key, to_ingredient_codes

# 심각도 — 모든 경고를 다 붙이면 답변이 경고문으로 뒤덮여
# 정작 물어본 것에 대한 답이 묻히고 관련성 항목에서 깎인다.
SEVERITY: dict[str, int] = {
    "병용금기": 100,
    "임부금기": 95,
    "특정연령대금기": 90,
    "용량주의": 60,
    "투여기간주의": 55,
    "노인주의": 50,
    "효능군중복": 45,
    "서방정분할주의": 30,
    "위험인자": 70,
}
ABSOLUTE = 90

_DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|밀리그램|g|그램|알|정|캡슐|포)")
_DURATION = re.compile(r"(\d+)\s*(일|주|주일|개월|달)\s*(?:째|동안|간|이상|넘게)")
_SPLIT = re.compile(r"(반으로|반씩|쪼개|나눠서|잘라|분할)")


def parse_dose(text: str) -> str | None:
    m = _DOSE.search(text)
    return m.group(0) if m else None


def parse_duration(text: str) -> str | None:
    m = _DURATION.search(text)
    return m.group(0) if m else None


def wants_split(text: str) -> bool:
    return bool(_SPLIT.search(text))


# ─────────────────────────────────────────────────────────────
# API 어댑터 — TODO(현장)
# ─────────────────────────────────────────────────────────────
class DurClient:
    """대회 엔드포인트 래퍼.

    현장 확인 항목
      [ ] 입력이 성분코드인가 품목기준코드인가 제품명 문자열도 받는가
      [ ] 8종이 각각 다른 엔드포인트인가 하나로 통합인가
      [ ] 쌍(pair) 단위인가 리스트를 한 번에 받는가
          ← 리스트를 받으면 조합 폭발 문제가 통째로 사라진다
      [ ] 응답에 심각도·등급 필드가 있는가 (없으면 위 SEVERITY 표를 쓴다)
      [ ] 제품→성분 전개를 API가 해주는가 우리가 해야 하는가
      [ ] rate limit — 쌍 조회를 병렬로 던져도 되는가
    """

    ENDPOINT = ""      # TODO(현장)

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client

    @property
    def live(self) -> bool:
        return bool(self.ENDPOINT) and self.client is not None

    async def pair(self, a: str, b: str) -> list[SafetyHit]:
        """병용금기 + 효능군중복 (쌍 단위)."""
        if not self.live:
            return _mock_pair(a, b)
        # TODO(현장): 요청/응답 스키마 확인
        r = await self.client.post(self.ENDPOINT, json={"type": "병용금기", "codes": [a, b]})
        r.raise_for_status()
        return [
            SafetyHit(kind="병용금기", severity=SEVERITY["병용금기"],
                      drugs=[a, b], reason=str(it.get("금기내용", "")))
            for it in (r.json().get("results") or [])
        ]

    async def single(self, code: str, kind: str, **ctx) -> list[SafetyHit]:
        """단일 약물 점검 (연령·임부·용량·기간·노인·분할)."""
        if not self.live:
            return _mock_single(code, kind, **ctx)
        r = await self.client.post(self.ENDPOINT, json={"type": kind, "code": code, **ctx})
        r.raise_for_status()
        return [
            SafetyHit(kind=kind, severity=SEVERITY.get(kind, 50),  # type: ignore[arg-type]
                      drugs=[code], reason=str(it.get("금기내용", "")))
            for it in (r.json().get("results") or [])
        ]


# ─────────────────────────────────────────────────────────────
# mock — 엔드포인트 전에도 파이프라인이 돌게
# ─────────────────────────────────────────────────────────────
_MOCK_PAIRS = {
    frozenset({"아세트아미노펜", "이부프로펜"}): ("효능군중복", "해열진통 성분이 중복되어 용량 초과 위험"),
    frozenset({"와파린", "아스피린"}): ("병용금기", "출혈 위험이 유의하게 증가"),
}


def _mock_pair(a: str, b: str) -> list[SafetyHit]:
    hit = _MOCK_PAIRS.get(frozenset({a, b}))
    if not hit:
        return []
    kind, reason = hit
    return [SafetyHit(kind=kind, severity=SEVERITY[kind], drugs=[a, b], reason=reason)]  # type: ignore[arg-type]


def _mock_single(code: str, kind: str, **ctx) -> list[SafetyHit]:
    if kind == "임부금기" and code in {"이부프로펜", "아스피린"}:
        return [SafetyHit(kind="임부금기", severity=SEVERITY["임부금기"], drugs=[code],
                          reason="임신부에게 투여하지 않는다")]
    if kind == "용량주의" and code == "아세트아미노펜":
        return [SafetyHit(kind="용량주의", severity=SEVERITY["용량주의"], drugs=[code],
                          reason="1일 최대 4000mg을 초과하지 않는다")]
    return []


# ─────────────────────────────────────────────────────────────
# 게이트 본체
# ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=8192)
def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


async def check(
    drug_names: list[str],
    session: SessionState,
    text: str,
    dur: DurClient,
    *,
    source: str = "input",
) -> list[SafetyHit]:
    """8종을 각각의 트리거에 따라 검사한다.

    복합제 전개가 중요하다. "감기약 + 두통약"은 제품명으로는 서로 다른 약이지만
    성분으로 펼치면 아세트아미노펜이 양쪽에 들어 있어 중복·용량초과가 된다.
    """
    if not drug_names:
        return []

    # 제품/효능군 → 성분코드 (1:N 전개)
    codes = sorted(to_ingredient_codes(drug_names) | {key(n) for n in drug_names})
    codes = [c for c in codes if c]
    if not codes:
        codes = [key(n) for n in drug_names]

    tasks = []

    # ① 쌍 단위 — 병용금기 · 효능군중복
    if len(codes) >= 2:
        pairs = {_pair_key(a, b) for a, b in itertools.combinations(codes, 2)}
        tasks += [dur.pair(a, b) for a, b in pairs]

    # ② 단일 약물에도 걸리는 것들
    dose = parse_dose(text)
    duration = parse_duration(text)
    split = wants_split(text)
    risk_types = {r.type for r in session.risk_factors}

    for c in codes:
        if session.age is not None:
            tasks.append(dur.single(c, "특정연령대금기", age=session.age))
            if session.age >= 65 or "elderly" in risk_types:
                tasks.append(dur.single(c, "노인주의"))
        if session.pregnant or "pregnancy" in risk_types:
            tasks.append(dur.single(c, "임부금기"))
        if dose:
            tasks.append(dur.single(c, "용량주의", dose=dose))
        if duration:
            tasks.append(dur.single(c, "투여기간주의", duration=duration))
        if split:
            tasks.append(dur.single(c, "서방정분할주의"))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    hits: list[SafetyHit] = []
    failed = False
    for r in results:
        if isinstance(r, list):
            hits.extend(r)
        else:
            failed = True

    if failed and not hits:
        # 침묵 금지 — "확인 못 했음"이 "문제 없음"으로 읽히면 안 된다
        hits.append(FALLBACK)

    for h in hits:
        h.source = source  # type: ignore[assignment]
    return dedupe(hits)


FALLBACK = SafetyHit(
    kind="위험인자",
    severity=40,
    reason=("여러 약을 함께 복용할 때는 성분이 겹치거나 상호작용이 있을 수 있으니, "
            "약사에게 복용 중인 약을 모두 알려주고 확인받으세요."),
)


def dedupe(hits: list[SafetyHit]) -> list[SafetyHit]:
    seen: set[tuple] = set()
    out: list[SafetyHit] = []
    for h in sorted(hits, key=lambda x: -x.severity):
        k = (h.kind, tuple(sorted(h.drugs)), h.risk_factor)
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def split_by_severity(hits: list[SafetyHit], notable_limit: int = 2):
    """절대 금기와 상대 주의는 처리가 달라야 한다.

    절대 금기에 경고만 삽입하면 자기모순이 된다:
      "숙취 두통엔 타이레놀이 좋습니다. ⚠️ 음주 후 타이레놀은 피하세요."
    → 절대 금기는 재작성, 상대 주의는 경고 삽입.
    """
    critical = [h for h in hits if h.severity >= ABSOLUTE]
    notable = [h for h in hits if h.severity < ABSOLUTE][:notable_limit]
    return critical, notable


def render_warning(hits: list[SafetyHit]) -> str:
    """LLM을 거치지 않는 고정 템플릿.

    안전 조치를 응답 맨 앞에 두면 +10, 뒤에 묻으면 -9.
    배치가 곧 점수인데 그걸 모델 재량에 맡길 이유가 없다.
    문자열 연결로 확정하면 100% 맨 앞이다.
    """
    if not hits:
        return ""
    lines = ["> ⚠️ **복용 전 확인이 필요합니다.**", ">"]
    for h in hits:
        who = " + ".join(h.drugs) if h.drugs else (h.risk_factor or "")
        head = f"**{who}**" if who else ""
        lines.append(f"> - {head} — {h.kind}: {h.reason}")
    lines.append(">")
    lines.append("> 해당하시면 **복용을 중단하고 약사 또는 의사에게 확인**하세요.")
    return "\n".join(lines)
