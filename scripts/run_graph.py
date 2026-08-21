"""LangGraph 경로로 한 턴 실행 — pipeline.py 와 결과가 같은지 확인용.

    python scripts/run_graph.py "어제 술 먹고 머리가 아파요"
    python scripts/run_graph.py --mermaid          # 그래프 구조를 mermaid 로 출력
    python scripts/run_graph.py --compare "질문"   # pipeline vs graph 결과 비교

LangGraph 가 없으면 명확한 안내와 함께 종료한다 (pipeline.py 로 쓰면 됨).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medai import config as cfgmod          # noqa: E402
from medai.contracts import SessionState    # noqa: E402
from medai.pipeline import Pipeline         # noqa: E402


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    cfg_path = "configs/mock.yaml"
    if "--config" in args:
        i = args.index("--config"); cfg_path = args[i + 1]; del args[i:i + 2]
    mermaid = "--mermaid" in args
    compare = "--compare" in args
    args = [a for a in args if not a.startswith("--")]
    text = args[0] if args else "어제 술을 너무 많이 마셨는데 머리가 아파요"

    cfg = cfgmod.load(cfg_path)
    pipe = Pipeline(cfg)

    try:
        from medai.graph import build_graph, run_turn as graph_turn
        app = build_graph(cfg, pipe)
    except ImportError as e:
        print(e)
        print("\n→ LangGraph 없이 쓰려면: python -m eval.harness --config configs/mock.yaml")
        return

    if mermaid:
        try:
            print(app.get_graph().draw_mermaid())
        except Exception as ex:
            print("mermaid 출력 실패:", ex)
        return

    print(f"[질문] {text}\n")
    out = await graph_turn(app, text, SessionState())
    print("[그래프 경로]")
    print("  red_flag :", out.get("red_flag").category if out.get("red_flag") else None)
    print("  intent   :", out["plan"].intent.value)
    print("  sources  :", out["plan"].sources)
    print("  ctx_docs :", len(out.get("context").docs) if out.get("context") else 0)
    print("  hits     :", [(h.kind, h.risk_factor) for h in (out.get("safety_hits") or [])])
    print("\n[답변]\n" + out["answer"][:800])

    if compare:
        res = await pipe.run_turn(text, SessionState())
        same = res.answer.strip() == out["answer"].strip()
        print("\n[비교] pipeline vs graph 결과 동일:", "✅" if same else "❌ 다름")
        if not same:
            print("--- pipeline ---\n" + res.answer[:400])


if __name__ == "__main__":
    asyncio.run(main())
