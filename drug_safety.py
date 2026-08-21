"""약물 안전 게이트 — 팀 하네스에 얹는 층.

★ 왜 필요한가
  submission/safety.py 는 **응급 전용**이다(119 안내). 약물 상호작용이 통째로 없다.
  실측으로 확인한 구멍:
    · "와파린 먹는데 아스피린 같이 먹어도 되나요"  → 출혈 위험 경고 없이 통과
    · "어제 술 먹고 두통약"                        → 음주+NSAID 위장출혈 경고 없음
    · "뭘 먹으면 좋을까요"                          → **모델이 추천한 약**을 아무도 검사 안 함

  대국민 챗봇에서 "이 약 먹어도 되나요"보다 "뭘 먹어야 하나요"가 더 흔하다.
  입력만 보면 더 큰 쪽을 놓친다. 그래서 **출력 텍스트**를 검사한다.

  채점상으로도 이쪽이 싸다 — 감점 항목은 분모에 없어서 가점으로 상쇄가 안 된다.

★ 설계 원칙
  · 완전 동기 · 외부 호출 없음 · 예외를 절대 밖으로 내지 않는다.
    답변을 다듬다 실패해서 답변 자체를 잃는 것이 최악이다.
  · 판정은 결정론(규칙·사전). LLM 을 부르지 않으므로 지연이 ~0 이고 재현된다.
  · 절대 금기(critical)와 상대 주의(notable)를 나눈다.
    절대 금기에 경고만 덧붙이면 "타이레놀 드세요 / 드시면 안 됩니다" 자기모순이 된다.

사용 (app.py 의 출구 한 곳에서):
    from drug_safety import guard
    content = guard(conversation_text, content)
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

# src/ 레이아웃의 medai 패키지를 쓴다 (Dockerfile 이 PYTHONPATH=/app/src 를 준다)
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

log = logging.getLogger("driver.drug_safety")

_READY = False
try:
    from medai import entities as ent
    from medai.contracts import RiskFactor, SafetyHit
    from medai.gates import dur as dur_mod
    from medai.gates import risk as risk_mod
    _READY = True
except Exception as e:            # 사전이 없어도 서버는 떠야 한다
    log.error("약물 안전 층 비활성 — import 실패: %r", e)


# ── 복용 중 vs 문의만 ────────────────────────────────────────
# "타이레놀 먹어도 되나요"(문의)를 복용 중으로 보면, 먹지도 않는 약으로
# 병용금기 경고가 나간다. 세션 복원과 같은 기준을 쓴다.
def _taking(text: str) -> bool:
    try:
        from medai import session as sess
        return bool(sess._TAKING.search(text))
    except Exception:
        return any(w in text for w in ("복용 중", "먹고 있", "복용하고 있", "처방받"))


# ── 권고 문맥만 남긴다 ───────────────────────────────────────
# 실측 사고: "이부프로펜·나프록센 등 NSAIDs 는 피하십시오" 문장에서 약을 뽑아
# 그 약으로 경고를 냈다. **모델이 먹지 말라고 한 약**을 우리가 다시 경고한 꼴이다.
# 회피·조건 문맥의 문장은 통째로 버리고, 실제로 권한 문장에서만 약을 뽑는다.
_AVOID = re.compile(
    r"피하|피해야|삼가|복용하지|드시지|먹지|금기|권하지 않|하지 마|하지마|"
    r"안 ?됩니다|안 ?돼|중복|초과하지|과용"
)
_COND = re.compile(r"라면|중인 경우|경우에는|병력|있으신 분|해당하")
# 대화 이력은 "role: content" 로 이어 붙여 들어온다. 역할 경계를 지켜야
# 봇이 한 말을 사용자가 한 말로 읽지 않는다.
_ROLE = re.compile(r"^(user|assistant|system)\s*:\s*")


def _recommended(answer: str) -> str:
    """답변에서 '이 약을 드세요' 쪽 문장만 이어 붙인다."""
    keep = [s for s in re.split(r"(?<=[.!?])\s+|\n", answer)
            if s and not _AVOID.search(s) and not _COND.search(s)]
    return "\n".join(keep)


def _pair_hits(taking: list[str], offered: list[str]) -> list[Any]:
    """약↔약 — 로컬 표로 쌍을 검사한다 (동기).

    ⚠️ 계열명을 대표 성분으로 확장한 코드는 절대 넣지 않는다.
       실측: 답변의 "소염진통제"→아스피린, "항응고제"→와파린 으로 부풀려진 뒤
       그 둘이 짝지어져 **존재하지도 않는 '아스피린+와파린 병용금기'** 가 나갔다.
       없는 금기를 경고하는 건 경고를 빠뜨리는 것보다 나쁘다(정확성 43%).

    그리고 한쪽은 반드시 **사용자가 복용 중이라고 밝힌 약**이어야 한다.
    답변 안의 두 약을 짝지으면 "A는 되고 B는 안 된다"가 병용으로 둔갑한다.

    dur_mod.check() 는 async 라 여기서 못 쓴다. 판정 본체인 _mock_pair 를 직접 쓴다.
    현장 DUR 엔드포인트가 열리면 이 함수만 그쪽으로 바꾸면 된다.
    """
    out: list[Any] = []
    for i, a in enumerate(taking):
        for b in taking[i + 1:]:                       # 복용 중인 약끼리
            out.extend(dur_mod._mock_pair(a, b))
        for b in offered:                              # 복용 중 × 새로 권한 약
            if a != b:
                out.extend(dur_mod._mock_pair(a, b))
    return out


def _single_hits(codes: list[str], risks: list[Any], text: str) -> list[Any]:
    """약 단독 — 임부금기·용량주의 등.

    용량주의는 사용자가 **실제 용량을 말했을 때만** 낸다. 예전엔 본문에 '최대'가
    있으면 냈는데, "타이레놀 최대 용량이 얼마인가요"라는 질문 자체에 걸려서
    답변이 이미 하는 말을 경고 상자로 한 번 더 하는 동어반복이 됐다.
    """
    out: list[Any] = []
    pregnant = any(getattr(r, "type", "") == "pregnancy" for r in risks)
    dosed = bool(dur_mod.parse_dose(text))
    for c in codes:
        if pregnant:
            out.extend(dur_mod._mock_single(c, "임부금기"))
        if dosed:
            out.extend(dur_mod._mock_single(c, "용량주의"))
    return out


def analyze(conversation_text: str, answer: str) -> list[Any]:
    """대화 + 답변에서 약물 안전 위반을 찾는다. 예외를 내지 않는다."""
    if not _READY:
        return []
    try:
        # ① 모델이 **권한** 약 — 이게 핵심이다 (회피 문맥은 걸러낸다)
        rec = _recommended(answer)
        offered_lit = ent.rule_extract_drugs(rec)              # 원문에 실재하는 이름만
        offered_cls = ent.rule_extract_classes(rec)            # 계열은 위험인자 대조에만 쓴다
        # ② 사용자가 복용 중이라고 밝힌 약 — **사용자 발화에서만** 뽑는다.
        #    실측 사고: 봇이 1턴에 "이부프로펜은 피하세요"라고 한 문장이 이력에 남아
        #    3턴에서 '복용 중'으로 읽혔고, 먹지도 않는 약 3개가 경고에 실렸다.
        in_names: set[str] = set()
        user_lines: list[str] = []
        role = "user"
        for line in conversation_text.splitlines():
            m = _ROLE.match(line)
            if m:
                role = m.group(1)
                line = line[m.end():]
            if role != "user":
                continue
            user_lines.append(line)
            if _taking(line):
                in_names |= ent.rule_extract_drugs(line)
        user_text = "\n".join(user_lines)
        # 계열명은 **구체적인 약을 하나도 못 찾았을 때만** 쓴다.
        #   실측: "아세트아미노펜을 드세요"라고만 한 답변에서 '진통제'라는 계열이
        #   같이 잡혀 이부프로펜·아스피린·나프록센까지 음주 경고에 실렸다.
        #   모델이 이미 "NSAIDs 는 피하라"고 말한 뒤였다 — 경고가 답변과 어긋난다.
        concrete = offered_lit | in_names
        names = sorted(concrete or offered_cls)
        if not names:
            return []

        # ③ 위험인자 — 사용자 발화 전체에서.
        #    1턴에 흘린 음주가 3턴 추천에 걸려야 한다. 다만 위험인자는 **사용자의 속성**이므로
        #    봇이 한 말에서 뽑으면 안 된다.
        #    실측 사고: 봇이 "간에 부담이 될 수 있습니다"라고 한 문장 때문에 간기능 저하가
        #    위험인자로 잡혀, 간 얘기를 한 적 없는 사용자에게 "간 기능이 저하된 경우" 경고가
        #    음주 경고와 나란히 두 줄로 나갔다.
        risks: list[Any] = []
        seen: set[str] = set()
        for r in ent.extract_risk_factors(user_text):
            if r.type not in seen:
                risks.append(r)
                seen.add(r.type)

        hits: list[Any] = []
        hits += risk_mod.check(names, risks)          # 약↔위험인자 (음주·임신·간·신장…)

        taking = sorted({ent.key(n) for n in in_names if ent.key(n)})
        offered = sorted({ent.key(n) for n in offered_lit if ent.key(n)})
        hits += _pair_hits(taking, offered)           # 약↔약 (리터럴 이름만)
        hits += _single_hits(sorted(set(taking + offered)), risks, user_text)
        return dur_mod.dedupe(hits)
    except Exception as e:
        log.error("약물 안전 분석 실패(무시하고 진행): %r", e)
        return []


def guard(conversation_text: str, answer: str) -> str:
    """답변에 안전 경고를 붙인다. 실패해도 원문을 그대로 돌려준다.

    경고는 **맨 앞**에 둔다 — 안전 안내는 앞에 있으면 가점, 뒤에 묻히면 감점이다.
    배치가 곧 점수인데 모델 재량에 맡길 이유가 없다.
    """
    if not answer:
        return answer
    try:
        hits = analyze(conversation_text, answer)
        if not hits:
            return answer
        critical, notable = dur_mod.split_by_severity(hits)
        show = critical + notable
        if not show:
            return answer
        warn = dur_mod.render_warning(show)
        if not warn or warn.split("\n")[0] in answer:
            return answer
        log.info("약물 안전 경고 %d건 삽입: %s",
                 len(show), [f"{h.kind}:{'+'.join(h.drugs)}" for h in show][:3])
        return warn + "\n\n" + answer
    except Exception as e:
        log.error("약물 안전 게이트 실패(원문 유지): %r", e)
        return answer
