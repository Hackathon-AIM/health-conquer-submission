"""대화형 챗 — 우리 챗봇과 직접 이야기해본다.

    .venv/bin/python scripts/chat.py                      # configs/l2_live.yaml
    .venv/bin/python scripts/chat.py --config configs/l2_raw.yaml   # 순수 L2 (비교용)
    .venv/bin/python scripts/chat.py --quiet              # 추적 정보 없이 답변만

명령어
    /reset    새 대화 (세션 초기화)
    /trace    직전 턴의 상세 추적
    /raw      순수 L2 와 우리 파이프라인을 같은 질문으로 비교
    /quit     종료

세션이 유지되므로 멀티턴을 실제로 시험할 수 있다.
"어제 술 먹고 머리 아파요" → "두통약 뭐 먹을까요" 처럼 이어서 물어보면
1턴 정보가 3턴 답변에 반영되는지 눈으로 확인된다.
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
from medai.contracts import SessionState    # noqa: E402
from medai.llm import LLM                   # noqa: E402
from medai.pipeline import Pipeline         # noqa: E402

DIM, BOLD, CYAN, YEL, RST = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


def show_trace(res, quiet: bool) -> None:
    if quiet:
        return
    t = res.trace or {}
    lat = res.latency_ms or {}
    plan = res.plan

    bits = [f"의도={getattr(plan.intent, 'value', plan.intent)}"]
    if getattr(plan, "depth", None):
        bits.append(f"깊이={plan.depth}")
    if t.get("red_flag"):
        bits.append(f"{YEL}응급={t['red_flag']}{DIM}")
    if getattr(plan, "need_followup", False):
        bits.append("되묻기=O")
    if plan.entities.drugs:
        bits.append("약(입력)=" + ",".join(d.name for d in plan.entities.drugs))
    if t.get("drugs_out"):
        bits.append("약(출력)=" + ",".join(t["drugs_out"]))

    l2 = (t.get("l2") or {}).get("retrieval") or {}
    if l2:
        bits.append(f"검색={l2.get('tool_calls')}회·{l2.get('status')}"
                    f"·근거{l2.get('cited')}개")

    print(f"{DIM}   ├ " + " │ ".join(bits))
    order = [k for k in ("l1", "l2", "l3_l1b", "l4", "l4b", "l4c") if lat.get(k)]
    print(f"{DIM}   └ " + " ".join(f"{k}:{lat[k]/1000:.1f}s" for k in order)
          + f"  {BOLD}합계 {lat.get('total', 0)/1000:.1f}s{RST}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/l2_live.yaml")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    cfg = cfgmod.load(a.config)
    llm = LLM(cfg)
    pipe = Pipeline(cfg, llm)

    raw_cfg = cfgmod.load("configs/l2_raw.yaml")
    raw_pipe = None   # /raw 를 처음 쓸 때 만든다

    print("=" * 68)
    print(f"  {BOLD}대화 시작{RST} — {a.config} ({cfg.get('name')})")
    print(f"  모델: {cfg['models']['drafter']}  |  "
          f"엔드포인트: {cfg['llm']['base_url'] or '(미설정 — 오프라인 스텁)'}")
    if not cfg["llm"]["base_url"]:
        print(f"  {YEL}⚠️  base_url 이 비어 오프라인 스텁이 답합니다. 실제 품질이 아닙니다.{RST}")
    print(f"  {DIM}/reset 새 대화 · /raw 순수L2 비교 · /trace 상세 · /quit 종료{RST}")
    print("=" * 68)

    session = SessionState()
    last = None
    turn = 0

    while True:
        try:
            text = input(f"\n{CYAN}{BOLD}나 ▶{RST} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료")
            return
        if not text:
            continue

        if text in ("/quit", "/q", "/exit"):
            print("종료")
            return
        if text == "/reset":
            session, turn, last = SessionState(), 0, None
            print(f"{DIM}   (세션 초기화){RST}")
            continue
        if text == "/trace":
            if last is None:
                print(f"{DIM}   (아직 턴이 없습니다){RST}")
                continue
            import json
            print(json.dumps(last.trace, ensure_ascii=False, indent=2))
            if last.plan.reasoning:
                print(f"\n분류 근거: {last.plan.reasoning}")
            if last.plan.clarifying_question:
                print(f"되물을 질문: {last.plan.clarifying_question}")
            continue
        if text.startswith("/raw"):
            q = text[4:].strip()
            if not q:
                print(f"{DIM}   사용법: /raw 질문내용{RST}")
                continue
            if raw_pipe is None:
                raw_pipe = Pipeline(raw_cfg, LLM(raw_cfg))
            t0 = time.perf_counter()
            r = await raw_pipe.run_turn(q, SessionState())
            print(f"\n{DIM}── 순수 L2 (파이프라인 없음) "
                  f"{(time.perf_counter()-t0):.1f}s ──{RST}")
            print(r.answer)
            continue

        turn += 1
        t0 = time.perf_counter()
        try:
            res = await pipe.run_turn(text, session)
        except Exception as e:
            print(f"\n{YEL}❌ 실패: {type(e).__name__}: {e}{RST}")
            continue
        last = res

        print(f"\n{BOLD}봇 ▶{RST} {res.answer}")
        show_trace(res, a.quiet)
        _ = time.perf_counter() - t0


if __name__ == "__main__":
    asyncio.run(main())
