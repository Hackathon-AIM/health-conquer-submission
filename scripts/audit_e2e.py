"""E2E 감사 — 다이어그램 노드가 코드에 실재하는지 대조한다.

    python scripts/audit_e2e.py

주장하지 말고 돌려서 확인한다. 아키텍처를 바꾸면 이 스크립트도 같이 고칠 것.
실제로 여기서 버그 3개가 나왔다:
  1. L4b risk_check 가 이번 턴의 위험인자를 못 봄 (sess.update 가 턴 끝에 호출됨)
  2. 응급 응답 빌더가 코드에 아예 없음 (템플릿 지시만 있고 강제 삽입 없음)
  3. 문의만 한 약이 복용 중으로 기록됨 (다음 턴 DUR 오판)
"""

import sys, asyncio, json, inspect
sys.path.insert(0, 'src')
from medai import config as c, entities as ent, redflag as rf, router, session as sess
from medai.pipeline import Pipeline
from medai.contracts import SessionState
from medai.gates import dur as durm, risk as riskm

cfg = c.load('configs/mock.yaml')
p = Pipeline(cfg)
OK, BAD = [], []
def chk(name, cond, note=""):
    (OK if cond else BAD).append(f"{name} {note}")
    print(("  ✅" if cond else "  ❌"), name, note)

print("="*72); print("A. 다이어그램 노드 ↔ 코드 대조"); print("="*72)

async def main():
    # ── 턴1: 증상 + 음주 (약물 없음)
    s = SessionState()
    r1 = await p.run_turn("어제 술을 너무 많이 마셨는데 머리가 너무 아파요", s)
    print("\n[턴1] 어제 술 + 두통")
    print("  plan.intent      :", r1.plan.intent.value)
    print("  entities.risk    :", [x.type for x in r1.plan.entities.risk_factors])
    print("  entities.temporal:", r1.plan.entities.temporal)
    print("  expects_drug_out :", r1.plan.expects_drug_output)
    print("  sources          :", r1.plan.sources)
    print("  drugs_out        :", r1.trace['drugs_out'])
    print("  safety_hits      :", [(h.kind, h.risk_factor, h.source) for h in r1.safety_hits])
    print("  latency          :", r1.latency_ms)
    chk("L1 risk_factors 추출", any(x.type=="alcohol" for x in r1.plan.entities.risk_factors))
    chk("L1 temporal 추출", r1.plan.entities.temporal is not None, f"({r1.plan.entities.temporal})")
    chk("L2 expects_drug_output", r1.plan.expects_drug_output)
    chk("L4b 출력측 약물 검출", len(r1.trace['drugs_out'])>0, str(r1.trace['drugs_out']))
    chk("L4b risk_check(음주) 발동", any(h.risk_factor=="alcohol" for h in r1.safety_hits))

    print("\n[세션 상태 · 턴1 후]")
    print("  risk_factors :", [x.type for x in s.risk_factors])
    print("  medications  :", s.medications)
    print("  med_names    :", s.medication_names)
    print("  duration     :", s.symptom_duration)
    print("  history len  :", len(s.history))
    chk("세션 risk 누적", any(x.type=="alcohol" for x in s.risk_factors))
    chk("세션 history 기록", len(s.history)==2)
    chk("세션 symptom_duration 저장", s.symptom_duration is not None)

    # ── 턴2: 복용약 밝힘
    r2 = await p.run_turn("사실 혈압약을 매일 먹고 있어요", s)
    print("\n[턴2] 혈압약 복용 밝힘")
    print("  med_names    :", s.medication_names)
    print("  medications  :", s.medications)
    print("  known_keys   :", s.known_keys())
    chk("세션 복용약 누적", len(s.medication_names)>0, str(s.medication_names))
    chk("세션 history 누적", len(s.history)==4, f"({len(s.history)})")

    # ── 턴3: 약 추천 요청 → L1b가 세션 복용약을 읽는가
    r3 = await p.run_turn("그럼 두통약은 뭘 먹으면 될까요?", s)
    print("\n[턴3] 약 추천 요청")
    print("  L1b input hits:", [(h.kind, h.drugs, h.source) for h in r3.safety_hits if h.source=="input"])
    print("  L4b out   hits:", [(h.kind, h.drugs, h.risk_factor) for h in r3.safety_hits if h.source=="output"])
    chk("L4b가 세션 위험인자 참조", any(h.risk_factor=="alcohol" for h in r3.safety_hits),
        "1턴 음주가 3턴 약추천에 반영")

    # ── 응급 경로
    print("\n[응급] 흉통")
    se = SessionState()
    re_ = await p.run_turn("갑자기 가슴이 조이고 식은땀이 나요", se)
    print("  red_flag  :", re_.trace['red_flag'])
    print("  sources   :", re_.plan.sources)
    print("  ctx_docs  :", re_.trace['ctx_docs'])
    print("  answer[:120]:", re_.answer[:120].replace("\n"," | "))
    chk("L1 레드플래그 HIT", re_.trace['red_flag']=="cardiac")
    chk("응급 시 L3 우회", re_.plan.sources==[] and re_.trace['ctx_docs']==0)
    has119 = any(w in re_.answer[:200] for w in ["119","응급실","즉시 내원","바로 병원"])
    chk("응급 응답 첫 문단에 119/응급실", has119, "← 다이어그램의 '응급 응답 빌더'")

asyncio.run(main())

print("\n"+"="*72); print("B. 규칙(rule-based) 정확성 — 적대적 입력"); print("="*72)
cases = [
  ("술→수술 오탐", "처치·수술 및 그 밖의 치료", lambda t: not any(x.type=="alcohol" for x in ent.extract_risk_factors(t))),
  ("술 정탐",       "어제 술을 마셨어요",        lambda t: any(x.type=="alcohol" for x in ent.extract_risk_factors(t))),
  ("신장(키) 오탐", "제 신장은 175cm예요",       lambda t: not any(x.type=="kidney" for x in ent.extract_risk_factors(t))),
  ("신장 정탐",     "신장 기능이 안 좋아요",      lambda t: any(x.type=="kidney" for x in ent.extract_risk_factors(t))),
  ("레드플래그 미탐", "타이레놀 몇 알 먹어요?",   lambda t: rf.scan(t) is None),
  ("레드플래그 정탐", "숨을 못 쉬겠어요",         lambda t: rf.scan(t) is not None),
  ("제형 접미사 정규화", "타이레놀정",            lambda t: ent.key(t)==ent.key("타이레놀")),
  ("제품 구분 유지", "타이레놀콜드",              lambda t: ent.key(t)!=ent.key("타이레놀")),
  ("복합제 전개",   "타이레놀콜드",              lambda t: len(ent.to_ingredient_codes([t]))>=2),
  ("효능군 확장",   "해열진통제",                lambda t: len(ent.expand_class(t))>=3),
]
for name, txt, fn in cases:
    chk(name, fn(txt), f'"{txt}"')

print("\n[LLM 환각 차단]")
chk("원문에 없는 약 거부", ent.merge_drug_candidates("머리가 아파요", set(),
    [{"name":"타이레놀","span":"타이레놀"}])==[])
chk("구어체 span 수용", [d.name for d in ent.merge_drug_candidates("타이레놀같은거 먹어도 돼요?", set(),
    [{"name":"타이레놀","span":"타이레놀같은거"}])]==["타이레놀"])
chk("사전에 없는 약 거부", ent.merge_drug_candidates("타이레날정 먹었어요", set(),
    [{"name":"타이레날정","span":"타이레날정"}])==[])

print("\n[DUR 8종 트리거]")
async def dur_cases():
    d = durm.DurClient(cfg)
    h1 = await durm.check(["와파린","아스피린"], SessionState(), "같이 먹어도?", d)
    chk("병용금기(약2개)", any(x.kind=="병용금기" for x in h1))
    h2 = await durm.check(["아세트아미노펜"], SessionState(), "하루 4000mg 먹어도?", d)
    chk("용량주의(약1개)", any(x.kind=="용량주의" for x in h2))
    h3 = await durm.check(["이부프로펜"], SessionState(pregnant=True), "먹어도?", d)
    chk("임부금기", any(x.kind=="임부금기" for x in h3))
asyncio.run(dur_cases())

print("\n[라우팅 표]")
from medai.contracts import QueryPlan, Entities, Intent, DrugMention
def rt(i, drugs=(), extra=()):
    return router.route(QueryPlan(intent=i, entities=Entities(drugs=[DrugMention(name=d) for d in drugs]),
                        extra_sources=list(extra)), cfg)
chk("symptom→guideline", rt(Intent.SYMPTOM_CONSULT)==["guideline"])
chk("drug_recommend→2소스", set(rt(Intent.DRUG_RECOMMEND))=={"guideline","drug"})
chk("info+약이름→drug추가", "drug" in rt(Intent.INFO_REQUEST, drugs=["타이레놀"]))
chk("info 약없음→drug제외", "drug" not in rt(Intent.INFO_REQUEST))
chk("policy→law+hira", set(rt(Intent.POLICY))=={"law","hira"})
chk("emergency→우회", rt(Intent.EMERGENCY)==[])
chk("extra_sources 반영", "pubmed" in rt(Intent.INFO_REQUEST, extra=["pubmed"]))

print("\n"+"="*72)
print(f"결과: {len(OK)} 통과 / {len(BAD)} 실패")
if BAD:
    print("\n❌ 실패 목록:")
    for b in BAD: print("   -", b)
import sys, asyncio, json, inspect
sys.path.insert(0, 'src')
from medai import config as c, entities as ent, redflag as rf, router, session as sess
from medai.pipeline import Pipeline
from medai.contracts import SessionState
from medai.gates import dur as durm, risk as riskm

cfg = c.load('configs/mock.yaml')
p = Pipeline(cfg)
OK, BAD = [], []
def chk(name, cond, note=""):
    (OK if cond else BAD).append(f"{name} {note}")
    print(("  ✅" if cond else "  ❌"), name, note)

print("="*72); print("A. 다이어그램 노드 ↔ 코드 대조"); print("="*72)

async def main():
    # ── 턴1: 증상 + 음주 (약물 없음)
    s = SessionState()
    r1 = await p.run_turn("어제 술을 너무 많이 마셨는데 머리가 너무 아파요", s)
    print("\n[턴1] 어제 술 + 두통")
    print("  plan.intent      :", r1.plan.intent.value)
    print("  entities.risk    :", [x.type for x in r1.plan.entities.risk_factors])
    print("  entities.temporal:", r1.plan.entities.temporal)
    print("  expects_drug_out :", r1.plan.expects_drug_output)
    print("  sources          :", r1.plan.sources)
    print("  drugs_out        :", r1.trace['drugs_out'])
    print("  safety_hits      :", [(h.kind, h.risk_factor, h.source) for h in r1.safety_hits])
    print("  latency          :", r1.latency_ms)
    chk("L1 risk_factors 추출", any(x.type=="alcohol" for x in r1.plan.entities.risk_factors))
    chk("L1 temporal 추출", r1.plan.entities.temporal is not None, f"({r1.plan.entities.temporal})")
    chk("L2 expects_drug_output", r1.plan.expects_drug_output)
    chk("L4b 출력측 약물 검출", len(r1.trace['drugs_out'])>0, str(r1.trace['drugs_out']))
    chk("L4b risk_check(음주) 발동", any(h.risk_factor=="alcohol" for h in r1.safety_hits))

    print("\n[세션 상태 · 턴1 후]")
    print("  risk_factors :", [x.type for x in s.risk_factors])
    print("  medications  :", s.medications)
    print("  med_names    :", s.medication_names)
    print("  duration     :", s.symptom_duration)
    print("  history len  :", len(s.history))
    chk("세션 risk 누적", any(x.type=="alcohol" for x in s.risk_factors))
    chk("세션 history 기록", len(s.history)==2)
    chk("세션 symptom_duration 저장", s.symptom_duration is not None)

    # ── 턴2: 복용약 밝힘
    r2 = await p.run_turn("사실 혈압약을 매일 먹고 있어요", s)
    print("\n[턴2] 혈압약 복용 밝힘")
    print("  med_names    :", s.medication_names)
    print("  medications  :", s.medications)
    print("  known_keys   :", s.known_keys())
    chk("세션 복용약 누적", len(s.medication_names)>0, str(s.medication_names))
    chk("세션 history 누적", len(s.history)==4, f"({len(s.history)})")

    # ── 턴3: 약 추천 요청 → L1b가 세션 복용약을 읽는가
    r3 = await p.run_turn("그럼 두통약은 뭘 먹으면 될까요?", s)
    print("\n[턴3] 약 추천 요청")
    print("  L1b input hits:", [(h.kind, h.drugs, h.source) for h in r3.safety_hits if h.source=="input"])
    print("  L4b out   hits:", [(h.kind, h.drugs, h.risk_factor) for h in r3.safety_hits if h.source=="output"])
    chk("L4b가 세션 위험인자 참조", any(h.risk_factor=="alcohol" for h in r3.safety_hits),
        "1턴 음주가 3턴 약추천에 반영")

    # ── 응급 경로
    print("\n[응급] 흉통")
    se = SessionState()
    re_ = await p.run_turn("갑자기 가슴이 조이고 식은땀이 나요", se)
    print("  red_flag  :", re_.trace['red_flag'])
    print("  sources   :", re_.plan.sources)
    print("  ctx_docs  :", re_.trace['ctx_docs'])
    print("  answer[:120]:", re_.answer[:120].replace("\n"," | "))
    chk("L1 레드플래그 HIT", re_.trace['red_flag']=="cardiac")
    chk("응급 시 L3 우회", re_.plan.sources==[] and re_.trace['ctx_docs']==0)
    has119 = any(w in re_.answer[:200] for w in ["119","응급실","즉시 내원","바로 병원"])
    chk("응급 응답 첫 문단에 119/응급실", has119, "← 다이어그램의 '응급 응답 빌더'")

asyncio.run(main())

print("\n"+"="*72); print("B. 규칙(rule-based) 정확성 — 적대적 입력"); print("="*72)
cases = [
  ("술→수술 오탐", "처치·수술 및 그 밖의 치료", lambda t: not any(x.type=="alcohol" for x in ent.extract_risk_factors(t))),
  ("술 정탐",       "어제 술을 마셨어요",        lambda t: any(x.type=="alcohol" for x in ent.extract_risk_factors(t))),
  ("신장(키) 오탐", "제 신장은 175cm예요",       lambda t: not any(x.type=="kidney" for x in ent.extract_risk_factors(t))),
  ("신장 정탐",     "신장 기능이 안 좋아요",      lambda t: any(x.type=="kidney" for x in ent.extract_risk_factors(t))),
  ("레드플래그 미탐", "타이레놀 몇 알 먹어요?",   lambda t: rf.scan(t) is None),
  ("레드플래그 정탐", "숨을 못 쉬겠어요",         lambda t: rf.scan(t) is not None),
  ("제형 접미사 정규화", "타이레놀정",            lambda t: ent.key(t)==ent.key("타이레놀")),
  ("제품 구분 유지", "타이레놀콜드",              lambda t: ent.key(t)!=ent.key("타이레놀")),
  ("복합제 전개",   "타이레놀콜드",              lambda t: len(ent.to_ingredient_codes([t]))>=2),
  ("효능군 확장",   "해열진통제",                lambda t: len(ent.expand_class(t))>=3),
]
for name, txt, fn in cases:
    chk(name, fn(txt), f'"{txt}"')

print("\n[LLM 환각 차단]")
chk("원문에 없는 약 거부", ent.merge_drug_candidates("머리가 아파요", set(),
    [{"name":"타이레놀","span":"타이레놀"}])==[])
chk("구어체 span 수용", [d.name for d in ent.merge_drug_candidates("타이레놀같은거 먹어도 돼요?", set(),
    [{"name":"타이레놀","span":"타이레놀같은거"}])]==["타이레놀"])
chk("사전에 없는 약 거부", ent.merge_drug_candidates("타이레날정 먹었어요", set(),
    [{"name":"타이레날정","span":"타이레날정"}])==[])

print("\n[DUR 8종 트리거]")
async def dur_cases():
    d = durm.DurClient(cfg)
    h1 = await durm.check(["와파린","아스피린"], SessionState(), "같이 먹어도?", d)
    chk("병용금기(약2개)", any(x.kind=="병용금기" for x in h1))
    h2 = await durm.check(["아세트아미노펜"], SessionState(), "하루 4000mg 먹어도?", d)
    chk("용량주의(약1개)", any(x.kind=="용량주의" for x in h2))
    h3 = await durm.check(["이부프로펜"], SessionState(pregnant=True), "먹어도?", d)
    chk("임부금기", any(x.kind=="임부금기" for x in h3))
asyncio.run(dur_cases())

print("\n[라우팅 표]")
from medai.contracts import QueryPlan, Entities, Intent, DrugMention
def rt(i, drugs=(), extra=()):
    return router.route(QueryPlan(intent=i, entities=Entities(drugs=[DrugMention(name=d) for d in drugs]),
                        extra_sources=list(extra)), cfg)
chk("symptom→guideline", rt(Intent.SYMPTOM_CONSULT)==["guideline"])
chk("drug_recommend→2소스", set(rt(Intent.DRUG_RECOMMEND))=={"guideline","drug"})
chk("info+약이름→drug추가", "drug" in rt(Intent.INFO_REQUEST, drugs=["타이레놀"]))
chk("info 약없음→drug제외", "drug" not in rt(Intent.INFO_REQUEST))
chk("policy→law+hira", set(rt(Intent.POLICY))=={"law","hira"})
chk("emergency→우회", rt(Intent.EMERGENCY)==[])
chk("extra_sources 반영", "pubmed" in rt(Intent.INFO_REQUEST, extra=["pubmed"]))

print("\n"+"="*72)
print(f"결과: {len(OK)} 통과 / {len(BAD)} 실패")
if BAD:
    print("\n❌ 실패 목록:")
    for b in BAD: print("   -", b)
