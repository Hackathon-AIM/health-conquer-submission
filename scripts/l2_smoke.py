"""L2 전 구간 스모크 — "나머지도 잘 되는지"를 한 방에 검사한다.

    cd /Users/ziuuu/Documents/med_ai
    python scripts/l2_smoke.py

검사 순서 (뒤로 갈수록 통합적):
  1. 모델 엔드포인트 연결 (/v1/models)
  2. 일반 chat 1회 + <think> 블록 여부
  3. ★ tool_calls — L2 가 우리가 주입한 도구를 OpenAI 형식으로 부르는가
     (playbook §7 의 1번 리스크. 여기서 실패하면 아키텍처 분기)
  4. 검색 단계 단독 (MCP 도구 루프 + finalize_retrieval)
  5. 2단계 전체 (generate: 검색이 필요한 질문)
  6. 파이프라인 전체 (L1 레드플래그 → … → L4b) — configs/l2_live.yaml

결과는 화면 + data/probe/l2_smoke.json 에 저장된다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod                    # noqa: E402
from medai.contracts import Intent, SessionState      # noqa: E402
from medai.llm import LLM                             # noqa: E402

OUT = ROOT / "data" / "probe" / "l2_smoke.json"
R: list[dict] = []


def rec(step: str, ok: bool, note: str = "", detail=None) -> None:
    R.append({"step": step, "ok": ok, "note": note, "detail": detail})
    print(f"  {'✅' if ok else '❌'} {step}" + (f" — {note}" if note else ""))


async def main() -> None:
    cfg = cfgmod.load("configs/l2_live.yaml")
    llm = LLM(cfg)
    if not llm.enabled:
        print("❌ base_url/openai 클라이언트가 안 잡혔습니다. .env 의 LUNIT_FM_API_KEY 확인.")
        return

    # 1. 연결 — 502(nginx bad gateway)는 저쪽 인프라의 간헐적 오류다. 기다렸다 재시도.
    ids: list = []
    for attempt in range(1, 7):
        try:
            t0 = time.perf_counter()
            models = await llm.client.models.list()
            ids = [m.id for m in models.data]
            rec("1. 모델 엔드포인트", True,
                f"{int((time.perf_counter()-t0)*1000)}ms — {ids[:5]}"
                + (f" (재시도 {attempt}회)" if attempt > 1 else ""), ids)
            break
        except Exception as e:
            transient = any(s in repr(e) for s in ("502", "503", "504", "Bad Gateway"))
            if transient and attempt < 6:
                wait = 10 * attempt
                print(f"  ⏳ 서버 502/5xx — {wait}초 후 재시도 ({attempt}/5) …")
                await asyncio.sleep(wait)
                continue
            rec("1. 모델 엔드포인트", False, repr(e)[:200])
            if transient:
                print("\n  → 루닛 서버 쪽 게이트웨이 오류입니다. 우리 코드 문제가 아닙니다.")
                print("     몇 분 뒤 다시 돌리거나, 운영진에게 모델 서버 상태를 확인하세요.")
            return
    if ids and "Lunit/L2-preview" not in ids:
        rec("1b. 모델 이름 확인", False,
            f"Lunit/L2-preview 가 목록에 없음! 실제: {ids} — configs 수정 필요")

    # 2. 일반 chat + think 여부
    try:
        t0 = time.perf_counter()
        r = await llm.client.chat.completions.create(
            model=llm.models["drafter"],
            messages=[{"role": "user", "content": "한 문장으로 인사해주세요."}],
            max_tokens=512)
        raw = r.choices[0].message.content or ""
        rec("2. 일반 chat", bool(raw.strip()),
            f"{int((time.perf_counter()-t0)*1000)}ms · think블록={'<think>' in raw.lower()}",
            raw[:200])
    except Exception as e:
        rec("2. 일반 chat", False, repr(e))

    # 3. ★ tool_calls 지원 — 아키텍처 분기점
    try:
        weather_tool = {"type": "function", "function": {
            "name": "lookup_drug", "description": "약 정보를 조회한다",
            "parameters": {"type": "object",
                           "properties": {"drug_name": {"type": "string"}},
                           "required": ["drug_name"]}}}
        r = await llm.client.chat.completions.create(
            model=llm.models["drafter"],
            messages=[{"role": "system",
                       "content": "필요하면 도구를 호출하세요."},
                      {"role": "user",
                       "content": "타이레놀 허가 정보를 도구로 조회해 주세요."}],
            tools=[weather_tool], max_tokens=512)
        tc = r.choices[0].message.tool_calls
        if tc:
            rec("3. tool_calls 형식", True,
                f"{tc[0].function.name}({tc[0].function.arguments[:80]})")
        else:
            rec("3. tool_calls 형식", False,
                "도구를 안 부르고 말로 답함 — tool_choice='required' 재시도")
            r2 = await llm.client.chat.completions.create(
                model=llm.models["drafter"],
                messages=[{"role": "user", "content": "타이레놀 정보를 조회하세요."}],
                tools=[weather_tool], tool_choice="required", max_tokens=512)
            tc2 = r2.choices[0].message.tool_calls
            rec("3b. tool_choice=required", bool(tc2),
                tc2[0].function.name if tc2 else "그래도 안 부름 — 프롬프트 기반 폴백 필요")
    except Exception as e:
        rec("3. tool_calls 형식", False,
            f"{type(e).__name__}: {e} — 서버가 tools 파라미터 자체를 거부하면 프롬프트 기반 폴백 필요")

    # 4. 검색 단계 단독
    from medai.l2 import L2Harness
    h = L2Harness(cfg, llm)
    try:
        t0 = time.perf_counter()
        r = await h.retrieval_stage(
            "만성 신장질환 성인의 권고 혈압 목표", Intent.SYMPTOM_CONSULT)
        rec("4. 검색 단계", bool(r["blocks"]) or r["status"] == "no_evidence",
            f"{int((time.perf_counter()-t0)*1000)}ms · status={r['status']} · "
            f"근거 {len(r['blocks'])}블록 · trace={h.trace.get('retrieval')}",
            {"note": r["note"], "first_block": (r["blocks"][0][:300] if r["blocks"] else None)})
    except Exception as e:
        rec("4. 검색 단계", False, repr(e))

    # 5. 2단계 전체
    try:
        t0 = time.perf_counter()
        ans = await h.generate("가이드라인 기준으로 만성 신장질환 환자의 혈압 목표가 어떻게 되나요?",
                               intent=Intent.SYMPTOM_CONSULT)
        ok = bool(ans.strip()) and "오류" not in ans[:30]
        rec("5. 2단계 generate", ok,
            f"{int((time.perf_counter()-t0)*1000)}ms · {len(ans)}자 · "
            f"인용번호포함={'[1]' in ans}", ans[:400])
    except Exception as e:
        rec("5. 2단계 generate", False, repr(e))

    # 6. 파이프라인 전체 (레드플래그 경로 포함)
    try:
        from medai.pipeline import Pipeline
        p = Pipeline(cfg, llm)
        t0 = time.perf_counter()
        res = await p.run_turn("어제 술을 많이 마셨는데 머리가 아파요. 약 뭐 먹을까요?",
                               SessionState())
        ok = bool(res.answer.strip())
        rec("6. 파이프라인 전체", ok,
            f"{int((time.perf_counter()-t0)*1000)}ms · latency={res.latency_ms} · "
            f"trace.l2={res.trace.get('l2')}",
            res.answer[:400])
    except Exception as e:
        rec("6. 파이프라인 전체", False, repr(e))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for x in R if x["ok"])
    print(f"\n{'='*60}\n  {n_ok}/{len(R)} 통과 → data/probe/l2_smoke.json 저장")
    if n_ok < len(R):
        print("  실패 항목의 detail 을 Claude 에게 보여주면 바로 고칠 수 있다")


if __name__ == "__main__":
    asyncio.run(main())
