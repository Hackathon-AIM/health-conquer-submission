"""현장 첫 60분 스모크 테스트.

여기서 막히면 나머지가 전부 멈춘다. 오프닝 끝나자마자 이것부터.

    python scripts/smoke.py configs/live.yaml

⚠️ 설계 원칙: 거짓 통과를 내지 않는다.
   base_url 이 비어 있으면 오프라인 스텁이 답하므로 JSON 테스트가 5/5로 '통과'한다.
   그건 모델 능력과 아무 상관이 없다. 체크리스트의 JSON 항목은 아키텍처 분기점이라
   여기서 거짓 통과가 나면 현장에서 크게 물린다.
   → 검증 불가한 항목은 PASS 가 아니라 SKIP 으로 표시한다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod          # noqa: E402
from medai.llm import LLM                   # noqa: E402
from medai.sources import build             # noqa: E402

PASS, FAIL, SKIP, WARN = "✅ PASS", "❌ FAIL", "⏭  SKIP", "⚠️  WARN"
_results: list[tuple[str, str, str]] = []


def mark(item: str, status: str, note: str = "") -> None:
    _results.append((item, status, note))
    print(f"  {status}  {item}" + (f"  — {note}" if note else ""))


async def check_fm(cfg, llm: LLM) -> None:
    print("\n[FM 엔드포인트]")
    if not llm.enabled:
        mark("FM 연결", SKIP, "base_url 미설정 — 아래 FM 항목은 전부 검증 불가")
        return

    t = time.perf_counter()
    out = await llm.chat("drafter", [{"role": "user", "content": "한 문장으로 인사해주세요."}])
    ms = int((time.perf_counter() - t) * 1000)
    mark("FM 연결", PASS if out else FAIL, f"{ms}ms")
    print(f"      └ {out[:120]}")

    # JSON 안정성 — 아키텍처 분기점
    ok = 0
    for i in range(5):
        r = await llm.chat_json("classifier", [
            {"role": "system", "content": '반드시 {"intent":"symptom_consult"} 형식의 JSON만 출력하세요.'},
            {"role": "user", "content": "머리가 아파요"},
        ])
        ok += bool(r)
    if ok == 5:
        mark("JSON 출력 5/5", PASS, "few-shot 만으로 충분")
    elif ok >= 3:
        mark(f"JSON 출력 {ok}/5", WARN, "few-shot 보강. 그래도 안 되면 constrained decoding")
    else:
        mark(f"JSON 출력 {ok}/5", FAIL,
             "vLLM + Outlines/xgrammar 검토. GPU 있으면 이게 가장 확실")

    # response_format 지원 여부
    try:
        await llm.client.chat.completions.create(   # type: ignore[union-attr]
            model=llm.models["classifier"],
            messages=[{"role": "user", "content": '{"a":1} 만 출력'}],
            max_tokens=16,
            response_format={"type": "json_object"},
        )
        mark("response_format 지원", PASS)
    except Exception as e:
        mark("response_format 지원", WARN, f"미지원 — few-shot 의존: {type(e).__name__}")

    try:
        models = await llm.client.models.list()     # type: ignore[union-attr]
        ids = [m.id for m in models.data][:5]
        mark("모델 목록 조회", PASS, ", ".join(ids))
    except Exception as e:
        mark("모델 목록 조회", WARN, type(e).__name__)

    print(f"      └ 설정된 context_window: {cfg['llm']['context_window']} "
          f"/ 컨텍스트 예산: {cfg['retrieval']['context_token_budget']}")
    print("        ※ 실제 값은 모델 config.json 의 max_position_embeddings 로 확인할 것")


async def check_sources(cfg) -> None:
    print("\n[RAG 검색 엔드포인트]")
    searchers = build(cfg)
    mode = cfg["retrieval"]["mode"]
    any_live = False
    for name, s in searchers.items():
        endpoint = getattr(s, "ENDPOINT", None)
        if mode == "live" and not endpoint:
            mark(name, SKIP, "sources/*.py 의 ENDPOINT 미설정")
            continue
        t = time.perf_counter()
        docs = await s.search("고혈압", 3)
        ms = int((time.perf_counter() - t) * 1000)
        if docs:
            any_live = True
            mark(name, PASS, f"{len(docs)}건 {ms}ms")
            d = docs[0]
            print(f"      └ {d.title[:34]} | locator={d.locator} | {d.text[:50]}")
        else:
            mark(name, FAIL if endpoint else SKIP, f"빈 결과 {ms}ms")
    if mode == "live" and not any_live:
        print("\n      → live 모드인데 살아있는 소스가 없습니다.")
        print("        mock 으로 개발을 계속하려면: MEDAI_RETRIEVAL_MODE=mock make run")


async def check_dur(cfg) -> None:
    print("\n[DUR 게이트]")
    from medai.contracts import SessionState
    from medai.gates import dur as dur_mod
    d = dur_mod.DurClient(cfg)
    if not d.live:
        mark("DUR 엔드포인트", SKIP,
             "gates/dur.py 의 DurClient.ENDPOINT 미설정 (mock 판정 사용 중)")
    hits = await dur_mod.check(["와파린", "아스피린"], SessionState(), "같이 먹어도 되나요", d)
    mark("DUR 판정 경로", PASS if hits else FAIL,
         f"{len(hits)}건 — {[h.kind for h in hits]}")


def check_data() -> None:
    print("\n[로컬 데이터]")
    from medai import entities as ent
    from medai import redflag as rf
    from medai.gates import risk as risk_mod

    dm, p2i, cls = ent.drug_map(), ent.product_to_ingredients(), ent.drug_class()
    mark("약물 사전", PASS if len(dm) > 200 else WARN,
         f"{len(dm)}건 — 시드 수준이면 확장: python data/build_dicts.py --csv 의약품목록.csv")
    mark("복합제 전개표", PASS if p2i else FAIL, f"{len(p2i)}건")
    mark("효능군 사전", PASS if cls else FAIL, f"{len(cls)}건")

    n_pat = sum(len(v) for v in rf._patterns().values())
    mark("레드플래그 패턴", WARN,
         f"{n_pat}개 / {len(rf.categories())}카테고리 — "
         "HealthBench emergency 테마에서 역추출해 교체할 것")

    mark("risk_check 규칙", PASS if risk_mod._rules() else FAIL,
         f"{len(risk_mod._rules())}개 성분 — 허가사항 '사용상의 주의사항' 으로 확장")


CHECKLIST = """
[ ] 1.  각 RAG 엔드포인트 응답 스키마 · 필드명 · top_k 최대 · rate limit · 지연
[ ] 2.  의약품 엔드포인트에 '사용상의 주의사항' 필드가 있는가  ← risk_check 원천
[ ] 3.  FM 컨텍스트 길이 (8K면 context_token_budget 을 2400 이하로)
[ ] 4.  FM 채팅 템플릿 형식 / instruction-tuned 여부
[ ] 5.  JSON 출력 5/5 인가                        ← 아키텍처 분기점
[ ] 6.  response_format={"type":"json_object"} 지원하는가
[ ] 7.  보조 작업(분류·비평·추출)에 FM 아닌 모델을 써도 되는가   ← 규정 확인
[ ] 8.  하네스·CoEval 타임아웃 설정 (timeout=, max_retries, 전체 실행 제한)
[ ] 9.  CoEval judge 모델이 무엇인가 (기본값 gpt-4.1) → models.grader 를 맞출 것
[ ] 10. 하네스의 환자 에이전트 시스템 프롬프트 읽기
[ ] 11. 문제 세트의 20%를 홀드아웃 격리
[ ] 12. DUR 엔드포인트: 성분코드인가 / 8종 통합인가 / 리스트를 한 번에 받는가
[ ] 13. 제출 방식 — CoEval 이 우리 serve.py 를 부르는가, 다른 형식인가
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default="configs/live.yaml")
    a = ap.parse_args()

    cfg = cfgmod.load(a.config)
    llm = LLM(cfg)

    print("=" * 68)
    print(f"  config    : {a.config}  ({cfg.get('name')})")
    print(f"  base_url  : {cfg['llm']['base_url'] or '(미설정)'}")
    print(f"  retrieval : {cfg['retrieval']['mode']}")
    print("=" * 68)

    if not llm.enabled:
        print("\n" + "!" * 68)
        print("  ⚠️  base_url 이 비어 있어 오프라인 스텁이 답합니다.")
        print("      아래 FM 관련 결과는 '검증된 것이 아닙니다'.")
        print("      특히 JSON 테스트는 통과해도 모델 능력과 무관합니다.")
        print()
        print("      실제 검증:")
        print("        export MEDAI_BASE_URL=https://.../v1")
        print("        export MEDAI_API_KEY=...")
        print("        make smoke")
        print("!" * 68)

    await check_fm(cfg, llm)
    await check_sources(cfg)
    await check_dur(cfg)
    check_data()

    n = {s: sum(1 for _, x, _ in _results if x == s) for s in (PASS, FAIL, WARN, SKIP)}
    print("\n" + "=" * 68)
    print(f"  PASS {n[PASS]}   FAIL {n[FAIL]}   WARN {n[WARN]}   SKIP {n[SKIP]}")
    if n[SKIP]:
        print("  ※ SKIP = 설정이 없어 '검증 불가'. 통과가 아닙니다.")
    print("=" * 68)
    print("현장에서 사람에게 물어봐야 하는 것" + CHECKLIST)


if __name__ == "__main__":
    asyncio.run(main())
