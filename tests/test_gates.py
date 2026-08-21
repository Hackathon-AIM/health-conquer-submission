"""결정론적 계층은 반드시 테스트가 있어야 한다.

L1 · L1b · L4b는 LLM 없이 돌아가므로 단위 테스트가 가능하고,
이게 "안전은 모델에 맡기지 않는다"를 실제로 보장하는 장치다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medai import config as cfgmod
from medai import entities as ent
from medai import redflag as rf
from medai import router
from medai.contracts import Entities, Intent, QueryPlan, RiskFactor, SessionState
from medai.gates import dur as dur_mod
from medai.gates import risk as risk_mod

CFG = cfgmod.load()


# ── L1 레드플래그 ────────────────────────────────────────────
def test_redflag_cardiac():
    hit = rf.scan("갑자기 가슴이 조이고 식은땀이 나요")
    assert hit is not None and hit.category == "cardiac"


def test_redflag_psych():
    assert rf.scan("요즘 그냥 죽고 싶어요") is not None


def test_redflag_negative():
    """평범한 질문에 응급이 걸리면 과탐지 — 모든 답변에 119가 붙어 관련성이 깎인다."""
    assert rf.scan("타이레놀 하루 몇 알까지 먹어도 되나요?") is None
    assert rf.scan("건강검진 보험 되나요?") is None


# ── L1 엔티티 ───────────────────────────────────────────────
def test_key_normalization():
    assert ent.key("타이레놀정") == ent.key("타이레놀")
    assert ent.key(" 타이레놀 ") == "타이레놀"


def test_key_keeps_product_distinction():
    """'타이레놀'과 '타이레놀콜드'를 합쳐버리면 용량 조언이 조용히 틀린다."""
    assert ent.key("타이레놀콜드") != ent.key("타이레놀")


def test_rule_extract_drug():
    assert "타이레놀" in ent.rule_extract_drugs("타이레놀 먹어도 되나요")


def test_risk_alcohol():
    risks = ent.extract_risk_factors("어제 술을 너무 많이 마셨어요")
    assert any(r.type == "alcohol" for r in risks)


def test_risk_elderly_from_age():
    assert any(r.type == "elderly" for r in ent.extract_risk_factors("제가 72세인데요"))


def test_temporal():
    assert ent.extract_temporal("2주째 아파요") is not None


# ── 합집합 + span 검증 (환각 차단) ──────────────────────────
def test_merge_rejects_hallucination():
    text = "머리가 아파요"
    out = ent.merge_drug_candidates(
        text, rule_hits=set(),
        llm_mentions=[{"name": "타이레놀", "span": "타이레놀"}],   # 원문에 없음
    )
    assert out == []


def test_merge_accepts_colloquial():
    text = "타이레놀같은거 먹어도 돼요?"
    out = ent.merge_drug_candidates(
        text, rule_hits=set(),
        llm_mentions=[{"name": "타이레놀", "span": "타이레놀같은거"}],
    )
    assert [d.name for d in out] == ["타이레놀"]


def test_merge_rejects_nonexistent_drug():
    text = "타이레날정 먹었어요"
    out = ent.merge_drug_candidates(
        text, rule_hits=set(),
        llm_mentions=[{"name": "타이레날정", "span": "타이레날정"}],
    )
    assert out == []


# ── 복합제 전개 ─────────────────────────────────────────────
def test_compound_expansion():
    """전개하지 않으면 '감기약 + 두통약'의 아세트아미노펜 중복을 놓친다."""
    codes = ent.to_ingredient_codes(["타이레놀콜드"])
    assert len(codes) >= 2


def test_class_expansion():
    """모호하게 말했을수록 넓게 판정해야 안전하다."""
    assert len(ent.expand_class("해열진통제")) >= 3


# ── L2 라우팅 ───────────────────────────────────────────────
def _plan(intent, drugs=(), extra=()):
    return QueryPlan(
        intent=intent,
        entities=Entities(drugs=[ent.DrugMention(name=d) for d in drugs]),
        extra_sources=list(extra),
    )


def test_route_symptom():
    assert router.route(_plan(Intent.SYMPTOM_CONSULT), CFG) == ["guideline"]


def test_route_drug_recommend_is_one_to_many():
    s = router.route(_plan(Intent.DRUG_RECOMMEND), CFG)
    assert set(s) == {"guideline", "drug"}


def test_route_entity_correction():
    """info_request 는 대상에 따라 소스가 달라진다 — 표만으로는 못 잡는다."""
    assert "drug" in router.route(_plan(Intent.INFO_REQUEST, drugs=["타이레놀"]), CFG)
    assert "drug" not in router.route(_plan(Intent.INFO_REQUEST), CFG)


def test_route_emergency_bypasses():
    assert router.route(_plan(Intent.EMERGENCY), CFG) == []


def test_route_extra_source():
    assert "pubmed" in router.route(_plan(Intent.INFO_REQUEST, extra=["pubmed"]), CFG)


def test_route_max_sources():
    p = _plan(Intent.POLICY, drugs=["타이레놀"], extra=["pubmed"])
    assert len(router.route(p, CFG)) <= CFG["routing"]["max_sources"]


# ── L1b DUR ────────────────────────────────────────────────
def test_dur_pair_interaction():
    dur = dur_mod.DurClient(CFG)
    hits = asyncio.run(dur_mod.check(
        ["와파린", "아스피린"], SessionState(), "같이 먹어도 되나요", dur))
    assert any(h.kind == "병용금기" for h in hits)


def test_dur_single_drug_still_checked():
    """'약 2개 이상이면 조회'는 틀렸다. 약 하나에도 5종이 걸린다."""
    dur = dur_mod.DurClient(CFG)
    hits = asyncio.run(dur_mod.check(
        ["아세트아미노펜"], SessionState(), "하루에 4000mg 먹어도 되나요", dur))
    assert any(h.kind == "용량주의" for h in hits)


def test_dur_pregnancy():
    dur = dur_mod.DurClient(CFG)
    hits = asyncio.run(dur_mod.check(
        ["이부프로펜"], SessionState(pregnant=True), "먹어도 되나요", dur))
    assert any(h.kind == "임부금기" for h in hits)


def test_severity_split():
    from medai.contracts import SafetyHit
    crit, note = dur_mod.split_by_severity([
        SafetyHit(kind="병용금기", severity=100),
        SafetyHit(kind="노인주의", severity=50),
    ])
    assert len(crit) == 1 and len(note) == 1


# ── L4b risk_check ─────────────────────────────────────────
def test_risk_alcohol_acetaminophen():
    """v1의 구멍: 약물이 입력이 아니라 출력에만 등장하는 경우."""
    hits = risk_mod.check(["아세트아미노펜"], [RiskFactor(type="alcohol")])
    assert hits and hits[0].risk_factor == "alcohol"


def test_risk_class_expands_broadly():
    """'해열진통제' + 음주 → 아세트아미노펜(간)·이부프로펜(위장) 등 여러 개가 걸려야 한다."""
    hits = risk_mod.check(["해열진통제"], [RiskFactor(type="alcohol")])
    assert len(hits) >= 2


def test_risk_no_factor_no_hit():
    assert risk_mod.check(["아세트아미노펜"], []) == []


def test_caution_screening():
    txt = "알코올을 상습적으로 섭취하는 환자에서는 간독성 위험이 증가할 수 있다"
    assert {"alcohol", "liver"} <= risk_mod.screen_caution_text(txt)


# ── 한국어 부분문자열 오탐 회귀 테스트 ──────────────────────
# 실제로 잡힌 버그: r"술" 이 "처치·수술 및 그 밖의 치료"(국민건강보험법 제41조)에
# 매칭되어 법률 답변에 음주 경고가 붙었다.
def test_alcohol_no_false_positive_on_surgery():
    assert not any(r.type == "alcohol" for r in ent.extract_risk_factors("처치·수술 및 그 밖의 치료"))


def test_alcohol_no_false_positive_on_common_words():
    for t in ["기술적인 문제", "시술 받은 뒤", "예술의전당 근처", "무술 배우다"]:
        assert not any(r.type == "alcohol" for r in ent.extract_risk_factors(t)), t


def test_alcohol_still_detected_with_particles():
    for t in ["어제 술을 너무 많이 마셨어요", "술 먹고 머리 아파요", "회식이 잦아요"]:
        assert any(r.type == "alcohol" for r in ent.extract_risk_factors(t)), t


def test_kidney_requires_context():
    assert any(r.type == "kidney" for r in ent.extract_risk_factors("신장 기능이 안 좋아요"))
    assert not any(r.type == "kidney" for r in ent.extract_risk_factors("제 신장은 175cm예요"))


# ── E2E 감사에서 발견된 3개 버그의 회귀 테스트 ──────────────────
def test_emergency_prefix_exists_for_all_redflag_categories():
    """다이어그램의 '응급 응답 빌더'는 코드에 실재해야 한다.

    템플릿 지시만 있고 강제 삽입이 없으면 LLM이 안 쓰면 그만이고, 그 순간 -9다.
    """
    from medai import generate as gen
    for cat in rf.categories():
        p = gen.emergency_prefix(cat)
        assert any(w in p for w in ["119", "응급실"]), cat
        assert p.startswith("**"), f"{cat}: 첫 문단이 굵게 강조되어야 한다"


def test_session_does_not_promote_asked_drug_to_medication():
    """'타이레놀 먹어도 되나요?'는 문의지 복용이 아니다.

    구분 없이 medication_names 에 넣으면 다음 턴 DUR 이 없는 금기를 만들어낸다.
    """
    from medai import session as S
    from medai.contracts import DrugMention, Entities, QueryPlan
    st = SessionState()
    plan = QueryPlan(entities=Entities(drugs=[DrugMention(name="타이레놀")]))
    S.update(st, "타이레놀 먹어도 되나요?", plan, "답변")
    assert st.medication_names == []
    assert "타이레놀" in st.asked_about


def test_session_promotes_when_actually_taking():
    from medai import session as S
    from medai.contracts import DrugMention, Entities, QueryPlan
    st = SessionState()
    plan = QueryPlan(entities=Entities(drugs=[DrugMention(name="타이레놀")]))
    S.update(st, "타이레놀 매일 먹고 있어요", plan, "답변")
    assert "타이레놀" in st.medication_names
