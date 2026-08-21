"""약물 안전 게이트 — 순수 stdlib. 외부 의존 0개.

★ 왜 자급자족인가
  이전 판은 src/medai 패키지(pydantic·pyyaml 필요)를 import 했고, 그 판이 평가에서
  `docker start --attach … exit 1` 로 죽었다. 네 번째였다. 컨테이너 로그를 못 봐
  원인을 끝내 특정하지 못했지만, 네 번 모두 공통점이 하나였다 —
  **우리가 이미지에 새 의존과 새 경로를 늘렸다는 것.**

  원인을 모르면 표면적을 줄이는 게 유일하게 정직한 대응이다. 그래서 이 파일은
    · import 가 re / logging / dataclasses 뿐 (전부 stdlib)
    · sys.path 를 건드리지 않는다
    · requirements.txt 를 건드리지 않는다
    · 파일을 읽지 않는다 (사전을 코드에 넣었다)
  app.py 옆에 놓기만 하면 되고, 빌드·기동에 아무것도 추가하지 않는다.

★ 무엇을 하는가
  대국민 챗봇에서는 "이 약 먹어도 되나요"보다 "뭘 먹으면 되나요"가 더 흔하다.
  그래서 검사 대상은 사용자 입력이 아니라 **모델이 내놓은 답변**이다.

★ 설계 원칙
  완전 동기 · 통신 없음 · 예외를 절대 밖으로 내지 않는다.
  답변을 다듬다 실패해서 답변 자체를 잃는 것이 최악이다.

사용 (app.py 출구 한 곳에서):
    from drug_safety import guard
    content = guard(conversation_text, content)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("driver.drug_safety")


# ── 사전 (data/drug_map.json · drug_class.json · risk_rules.yaml 인라인) ──
DRUG_MAP = {
    "타이레놀": "아세트아미노펜", "아세트아미노펜": "아세트아미노펜",
    "타이레놀콜드": "아세트아미노펜", "판피린": "아세트아미노펜",
    "게보린": "아세트아미노펜", "부루펜": "이부프로펜", "이부프로펜": "이부프로펜",
    "애드빌": "이부프로펜", "이지엔6": "이부프로펜", "아스피린": "아스피린",
    "낙센": "나프록센", "나프록센": "나프록센", "덱시부프로펜": "덱시부프로펜",
    "디클로페낙": "디클로페낙", "와파린": "와파린", "클로르페니라민": "클로르페니라민",
    "지르텍": "세티리진", "세티리진": "세티리진", "알레그라": "펙소페나딘",
    "펙소페나딘": "펙소페나딘", "클라리틴": "로라타딘", "로라타딘": "로라타딘",
    "겔포스": "수산화알루미늄", "수산화알루미늄": "수산화알루미늄",
    "탄산칼슘": "탄산칼슘", "베아제": "소화효소", "훼스탈": "소화효소",
    "암로디핀": "암로디핀", "노바스크": "암로디핀", "로사르탄": "로사르탄",
    "텔미사르탄": "텔미사르탄", "메트포르민": "메트포르민",
    "슈도에페드린": "슈도에페드린",
}

DRUG_CLASS = {
    "해열진통제": ["아세트아미노펜", "이부프로펜", "아스피린", "나프록센"],
    "소염진통제": ["이부프로펜", "나프록센", "덱시부프로펜"],
    "진통제": ["아세트아미노펜", "이부프로펜", "아스피린", "나프록센"],
    "NSAID": ["이부프로펜", "아스피린", "나프록센", "디클로페낙"],
    "비스테로이드성소염진통제": ["이부프로펜", "아스피린", "나프록센"],
    "아세트아미노펜 계열": ["아세트아미노펜"],
    "항히스타민제": ["세티리진", "로라타딘", "클로르페니라민", "펙소페나딘"],
    "제산제": ["수산화알루미늄", "탄산칼슘"],
    "항응고제": ["와파린"],
    "혈압약": ["암로디핀", "로사르탄", "텔미사르탄"],
    "항고혈압제": ["암로디핀", "로사르탄", "텔미사르탄"],
}

# ⚠️ 의학적 논쟁이 있는 항목은 단정하지 않는다. 조건을 밝혀야 불확실성 표현에서 가점이다.
RISK_RULES = {
    "아세트아미노펜": {
        "alcohol": ("음주, 특히 상습적인 음주 시 간에 부담이 될 수 있습니다. 위험도는 음주 습관과 "
                    "복용량에 따라 다르므로, 확실치 않으면 복용 전 약사와 상의하시는 편이 안전합니다."),
        "liver": "간 기능이 저하된 경우 대사가 지연될 수 있어 용량 조절이 필요할 수 있습니다.",
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
    "와파린": {
        "alcohol": "음주는 항응고 효과에 영향을 줄 수 있어 일정한 음주 습관 유지가 권장됩니다.",
        "liver": "간 기능에 따라 용량 조절이 필요합니다.",
    },
}

PAIRS = {
    frozenset({"와파린", "아스피린"}): ("병용금기", 100, "출혈 위험이 유의하게 증가"),
    frozenset({"아세트아미노펜", "이부프로펜"}): ("효능군중복", 45, "해열진통 성분이 중복되어 용량 초과 위험"),
}
PREGNANCY_BAN = {"이부프로펜", "아스피린"}
DOSE_CAUTION = {"아세트아미노펜": "1일 최대 4000mg 을 초과하지 않는다"}
ABSOLUTE = 90                       # 이 이상이면 절대 금기


# ── 정규식 ────────────────────────────────────────────────────
_RISK = {
    "alcohol": re.compile(r"(?<![가-힣])술(?![기의])|음주|소주|맥주|막걸리|와인|위스키|폭탄주|회식|숙취|해장|취했|취함|주량|과음"),
    "pregnancy": re.compile(r"임신|임산부|임부|태아|출산\s*후"),
    "lactation": re.compile(r"수유|모유|젖\s*(먹이|병)"),
    "liver": re.compile(r"간\s*(질환|기능|수치|손상|이\s*안\s*좋)|지방간|간염|간경화|간독성"),
    "kidney": re.compile(r"신장\s*(질환|기능|병|이|에|수치)|콩팥|투석|사구체|신부전"),
    "driving": re.compile(r"운전|기계\s*조작|작업\s*중"),
}
_AGE = re.compile(r"(\d{1,3})\s*(?:살|세)")
_SUFFIX = re.compile(r"(정|캡슐|캅셀|시럽|산|주|액|이알|서방정|연질캡슐|정제|현탁액)$")
_DOSE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|밀리그램|g|그램|알|정|캡슐|포)")

# "타이레놀 먹어도 되나요"(문의)를 복용 중으로 보면, 먹지도 않는 약으로 병용금기가 나간다.
_TAKING = re.compile(
    r"(먹고\s*있|복용\s*(중|하고)|드시고\s*있|처방\s*(받|중)|"
    r"매일\s*(먹|복용)|계속\s*(먹|복용)|장기\s*복용)"
)

# 이력은 "role: content" 로 들어온다. 역할 경계를 지켜야 봇이 한 말을 사용자 말로 안 읽는다.
_ROLE = re.compile(r"^(user|assistant|system)\s*:\s*")

# 회피·조건 문맥의 문장은 통째로 버린다.
#   실측: "이부프로펜·나프록센 등 NSAIDs 는 피하십시오" 에서 약을 뽑아 경고를 냈다.
#   모델이 먹지 말라고 한 약을 우리가 다시 경고한 꼴이다.
_AVOID = re.compile(
    r"피하|피해야|삼가|복용하지|드시지|먹지|금기|권하지 않|하지 마|하지마|"
    r"안 ?됩니다|안 ?돼|중복|초과하지|과용"
)
_COND = re.compile(r"라면|중인 경우|경우에는|병력|있으신 분|해당하")


@dataclass
class Hit:
    kind: str
    severity: int
    drugs: list = field(default_factory=list)
    risk_factor: str = ""
    reason: str = ""


def _key(s: str) -> str:
    """표기 흔들림 제거. 제형 접미사는 자르되 제품 구분 수식어는 남긴다."""
    return _SUFFIX.sub("", str(s).strip().lower().replace(" ", "").replace("·", ""))


_MAP_BY_KEY = {_key(k): v for k, v in DRUG_MAP.items()}


def _drugs_in(text: str) -> set:
    """사전에 있는 이름이 원문에 문자 그대로 있는 것만. 창작하지 않는다."""
    t = _key(text)
    return {v for k, v in _MAP_BY_KEY.items() if len(k) >= 2 and k in t}


def _risks_in(text: str) -> list:
    out = [t for t, p in _RISK.items() if p.search(text)]
    m = _AGE.search(text)
    if m:
        a = int(m.group(1))
        if a >= 65:
            out.append("elderly")
        elif a < 12:
            out.append("pediatric")
    return out


def _recommended(answer: str) -> str:
    """답변에서 '이 약을 드세요' 쪽 문장만 이어 붙인다."""
    keep = [s for s in re.split(r"(?<=[.!?])\s+|\n", answer)
            if s and not _AVOID.search(s) and not _COND.search(s)]
    return "\n".join(keep)


def analyze(conversation_text: str, answer: str) -> list:
    """대화 + 답변에서 약물 안전 위반을 찾는다. 예외를 내지 않는다."""
    try:
        # ① 모델이 **권한** 약 (회피 문맥은 걸러낸다)
        rec = _recommended(answer)
        offered = _drugs_in(rec)
        offered_cls = {c for c in DRUG_CLASS if c in rec}

        # ② 복용 중인 약 · 위험인자 — 둘 다 **사용자 발화에서만** 뽑는다.
        #    봇이 "간에 부담이 됩니다"라고 한 문장에서 간기능 저하를 위험인자로 읽으면,
        #    간 얘기를 한 적 없는 사람에게 경고가 나간다 (실측된 오탐).
        taking = set()
        user_lines = []
        role = "user"
        for line in conversation_text.splitlines():
            m = _ROLE.match(line)
            if m:
                role = m.group(1)
                line = line[m.end():]
            if role != "user":
                continue
            user_lines.append(line)
            if _TAKING.search(line):
                taking |= _drugs_in(line)
        user_text = "\n".join(user_lines)

        # 계열명은 구체적인 약을 하나도 못 찾았을 때만 쓴다.
        #   "아세트아미노펜을 드세요"만 한 답변에서 '진통제' 계열이 같이 잡혀
        #   이부프로펜·아스피린·나프록센까지 음주 경고에 실린 적이 있다.
        names = set(offered) | taking
        if not names:
            for c in offered_cls:
                names |= set(DRUG_CLASS[c])
        if not names:
            return []

        risks = _risks_in(user_text)
        hits = []

        # 약 ↔ 위험인자
        for n in sorted(names):
            rule = RISK_RULES.get(n)
            if not rule:
                continue
            for t in risks:
                if t in rule:
                    hits.append(Hit("위험인자", 70, [n], t, rule[t]))

        # 약 ↔ 약. 한쪽은 반드시 **사용자가 복용 중이라고 밝힌 약**이어야 한다.
        #   답변 안의 두 약을 짝지으면 "A는 되고 B는 안 된다"가 병용으로 둔갑한다.
        #   실측: 계열 확장까지 겹쳐 존재하지도 않는 '아스피린+와파린 병용금기'가 나갔다.
        for a in sorted(taking):
            for b in sorted(taking | offered):
                if a == b:
                    continue
                p = PAIRS.get(frozenset({a, b}))
                if p:
                    hits.append(Hit(p[0], p[1], sorted([a, b]), "", p[2]))

        # 약 단독. 용량주의는 사용자가 **실제 용량을 말했을 때만** —
        # "타이레놀 최대 용량이 얼마인가요"라는 질문 자체에 걸리면 동어반복이다.
        pregnant = "pregnancy" in risks
        dosed = bool(_DOSE.search(user_text))
        for n in sorted(names):
            if pregnant and n in PREGNANCY_BAN:
                hits.append(Hit("임부금기", 95, [n], "", "임신부에게 투여하지 않는다"))
            if dosed and n in DOSE_CAUTION:
                hits.append(Hit("용량주의", 60, [n], "", DOSE_CAUTION[n]))

        # 중복 제거 (심각도 높은 것부터)
        seen = set()
        out = []
        for h in sorted(hits, key=lambda x: -x.severity):
            k = (h.kind, tuple(sorted(h.drugs)), h.risk_factor)
            if k not in seen:
                seen.add(k)
                out.append(h)
        return out
    except Exception as e:              # noqa: BLE001 — 답변을 잃는 것이 최악이다
        log.error("약물 안전 분석 실패(무시하고 진행): %r", e)
        return []


def render_warning(hits: list) -> str:
    """LLM 을 거치지 않는 고정 템플릿. 문자열 연결이라 100% 맨 앞이다."""
    if not hits:
        return ""
    lines = ["> ⚠️ **복용 전 확인이 필요합니다.**", ">"]
    for h in hits:
        lines.append(f"> - **{' + '.join(h.drugs)}** — {h.kind}: {h.reason}")
    lines.append(">")
    lines.append("> 해당하시면 **복용을 중단하고 약사 또는 의사에게 확인**하세요.")
    return "\n".join(lines)


def guard(conversation_text: str, answer: str) -> str:
    """답변 맨 앞에 안전 경고를 붙인다. 실패해도 원문을 그대로 돌려준다.

    맨 앞에 두는 이유: 안전 안내는 앞에 있으면 가점, 뒤에 묻히면 감점이다.
    배치가 곧 점수인데 모델 재량에 맡길 이유가 없다.
    """
    if not answer:
        return answer
    try:
        hits = analyze(conversation_text, answer)
        if not hits:
            return answer
        # 절대 금기는 전부, 상대 주의는 2건까지. 경고가 답변보다 길면 읽히지 않는다.
        show = ([h for h in hits if h.severity >= ABSOLUTE]
                + [h for h in hits if h.severity < ABSOLUTE][:2])
        warn = render_warning(show)
        if not warn or warn.split("\n")[0] in answer:
            return answer
        log.info("약물 안전 경고 %d건 삽입: %s",
                 len(show), [f"{h.kind}:{'+'.join(h.drugs)}" for h in show][:3])
        return warn + "\n\n" + answer
    except Exception as e:              # noqa: BLE001
        log.error("약물 안전 게이트 실패(원문 유지): %r", e)
        return answer
