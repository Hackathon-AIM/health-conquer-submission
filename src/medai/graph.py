"""LangGraph 래퍼 — 선택적(optional).

pipeline.Pipeline 의 노드 메서드를 그대로 재사용하는 얇은 층이다.
로직은 한 곳(pipeline.py)에만 있고 여기는 배선만 한다.

  pipeline.Pipeline          graph.py 노드
  ──────────────────────     ─────────────────
  node_l1   (레드플래그)  →  l1_safety
  node_l2   (의도 분류)   →  l2_classify
  node_l3   (RAG 검색)  ┐
  node_l1b  (입력 DUR)  ┘ →  l3_retrieve_l1b_dur   ← 같은 gather (0.5초 절약)
  node_l4   (초안+응급)   →  l4_draft
  node_l4b  (출력 게이트) →  l4b_safety_gate
  node_l4c  (루브릭 비평) →  l4c_critic

왜 이렇게 나눴나
  · LangGraph 장점: 상태 모델, 노드 전환 가시성, LangSmith 관측성, 체크포인트, 스트리밍
  · LangGraph 위험: 20시간 해커톤에서 처음 배우면 그 자체가 리스크.
    버전 API가 자주 바뀌고 디버깅이 프레임워크 안으로 들어간다.
  → 노드를 순수 메서드로 두면 둘 다 가능하다. 문제가 생기면 Pipeline.run_turn 으로 폴백.

⚠️ 응급 분기
  레드플래그 HIT 시 L2·L3를 건너뛰어야 하므로 조건부 엣지를 쓴다.
  Pipeline.run_turn 은 node_l2 내부에서 처리하지만(EMERGENCY intent → sources=[]),
  그래프에서는 경로가 눈에 보이는 게 낫다.

사용:
    python scripts/run_graph.py "어제 술 먹고 머리가 아파요"
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, TypedDict

from .config import Config
from .contracts import Context, QueryPlan, RedFlagHit, SafetyHit, SessionState
from .pipeline import Pipeline


class GraphState(TypedDict, total=False):
    # 입력
    text: str
    session: SessionState
    # 계층별 산출물
    red_flag: Optional[RedFlagHit]
    plan: QueryPlan
    context: Context
    input_hits: list
    answer: str
    safety_hits: list
    mentioned: list
    critique: Any


def build_graph(cfg: Config, pipe: Pipeline | None = None):
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "LangGraph가 설치되어 있지 않습니다.\n"
            "  pip install langgraph\n"
            "또는 medai.pipeline.Pipeline 을 직접 쓰세요 (기능은 동일합니다)."
        ) from e

    p = pipe or Pipeline(cfg)

    # ── 노드: 전부 Pipeline 메서드를 부르기만 한다 ──────────
    async def l1_safety(s: GraphState) -> GraphState:
        return {"red_flag": p.node_l1(s["text"], s["session"])}

    async def l2_classify(s: GraphState) -> GraphState:
        return {"plan": await p.node_l2(s["text"], s["session"], s.get("red_flag"))}

    async def l3_retrieve_l1b_dur(s: GraphState) -> GraphState:
        # 서로 의존하지 않으므로 같은 gather. 0.5초가 공짜로 사라진다.
        ctx, hits = await asyncio.gather(
            p.node_l3(s["text"], s["plan"]),
            p.node_l1b(s["text"], s["plan"], s["session"]),
        )
        return {"context": ctx, "input_hits": hits}

    async def l4_draft(s: GraphState) -> GraphState:
        return {"answer": await p.node_l4(
            s["text"], s["plan"], s.get("context") or Context(),
            s["session"], s.get("input_hits") or [], s.get("red_flag"),
        )}

    async def l4b_safety_gate(s: GraphState) -> GraphState:
        answer, hits, mentioned = await p.node_l4b(
            s["text"], s["plan"], s["session"], s["answer"], s.get("input_hits") or []
        )
        return {"answer": answer, "safety_hits": hits, "mentioned": mentioned}

    async def l4c_critic(s: GraphState) -> GraphState:
        answer, crit = await p.node_l4c(
            s["text"], s["answer"], s["plan"], s.get("context") or Context(),
            s["session"], s.get("safety_hits") or [],
        )
        return {"answer": answer, "critique": crit}

    # ── 배선 ────────────────────────────────────────────────
    g = StateGraph(GraphState)
    g.add_node("l1_safety", l1_safety)
    g.add_node("l2_classify", l2_classify)
    g.add_node("l3_retrieve_l1b_dur", l3_retrieve_l1b_dur)
    g.add_node("l4_draft", l4_draft)
    g.add_node("l4b_safety_gate", l4b_safety_gate)
    g.add_node("l4c_critic", l4c_critic)

    g.add_edge(START, "l1_safety")
    g.add_edge("l1_safety", "l2_classify")

    # 응급이면 L3를 건너뛰고 바로 초안으로 (검색 우회)
    def route_after_classify(s: GraphState) -> str:
        return "l4_draft" if not s["plan"].sources else "l3_retrieve_l1b_dur"

    g.add_conditional_edges(
        "l2_classify", route_after_classify,
        {"l3_retrieve_l1b_dur": "l3_retrieve_l1b_dur", "l4_draft": "l4_draft"},
    )

    g.add_edge("l3_retrieve_l1b_dur", "l4_draft")
    g.add_edge("l4_draft", "l4b_safety_gate")
    g.add_edge("l4b_safety_gate", "l4c_critic")
    g.add_edge("l4c_critic", END)
    return g.compile()


async def run_turn(app, text: str, session: SessionState) -> GraphState:
    """그래프로 한 턴 실행 + 세션 갱신.

    Pipeline.run_turn 과 동일한 결과를 내되, 노드 전환이 그래프로 보인다.
    """
    from . import session as sess
    out = await app.ainvoke({"text": text, "session": session})
    sess.update(session, text, out["plan"], out["answer"])
    return out
