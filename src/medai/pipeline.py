"""오케스트레이션 — 순수 async 함수 체인.

LangGraph 없이도 이 파일만으로 완전히 동작한다.
graph.py 는 이 노드들을 감싸는 얇은 래퍼일 뿐이므로,
LangGraph가 현장에서 말썽을 부리면 그냥 이걸 쓰면 된다.

실행 순서 (다이어그램의 논리 순서)
  L1  엔티티 추출 + 레드플래그      결정론적 · ~0ms
  L2  의도 분류                    LLM 1회 · 1~2s
  L3  RAG 검색 (병렬)              1~3s   ┐ 서로 의존하지 않으므로
  L1b 입력측 DUR                   ~0.5s  ┘ 같은 gather에 넣어 동시 실행
  L4  초안 생성                    3~6s
  L4b 출력측 안전 게이트           ~0.5s
  L4c 루브릭 비평                  2~3s
                                   ────────
                                   8~15s

⚠️ 이 지연 예산은 설계 목표이지 대회 규정이 아니다.
   공고에 타임아웃 기준은 없다. 실제 이유는 개발 루프 회전 수
   (200문항×3턴 기준 실험 1회가 턴당 8초면 ~27분, 15초면 ~50분).
"""

from __future__ import annotations

import asyncio
import time

from . import classify as classify_mod
from . import generate as gen
from . import redflag as rf
from . import rerank as rr
from . import router
from . import session as sess
from . import sources as src
from .config import Config
from .contracts import Context, Intent, QueryPlan, SafetyHit, SessionState, TurnResult
from .critic import critique_loop
from .gates import dur as dur_mod
from .llm import LLM


class Pipeline:
    def __init__(self, cfg: Config, llm: LLM | None = None):
        self.cfg = cfg
        self.llm = llm or LLM(cfg)
        self.searchers = src.build(cfg)
        self.dur = dur_mod.DurClient(cfg)

        # ★ L2 네이티브 경로 — 대회 FM(L2)의 2단계(검색→생성) 권장 사용법.
        #   켜지면 L3(우리 검색)·rerank 를 건너뛰고 검색 주체가 L2 모델 자신이 된다.
        #   대회 규칙: 최종 출력물은 반드시 L2 로 생성.
        self.l2 = None
        if cfg["layers"].get("l2_native", False):
            from .l2 import L2Harness
            self.l2 = L2Harness(cfg, self.llm)
        else:
            # 리랭커는 여기서 한 번만 로딩한다 (실행 중 지연 로딩 금지)
            rr.preload(cfg)

    # ── 노드들 (LangGraph에서도 그대로 재사용) ──────────────
    def node_l1(self, text: str, session: SessionState):
        """L1 · 레드플래그. 엔티티 규칙 추출은 classify 내부에서 수행."""
        if not self.cfg["layers"].get("redflag", True):
            return None
        return rf.scan(text)

    async def node_l2(self, text, session, red_flag) -> QueryPlan:
        plan = await classify_mod.classify(text, session, self.llm, self.cfg, red_flag)
        plan.sources = router.route(plan, self.cfg)
        return plan

    async def node_l3(self, text: str, plan: QueryPlan) -> Context:
        if self.l2 is not None:
            return Context()   # L2 네이티브: 검색은 L2 가 생성 단계에서 스스로 한다
        if not plan.sources:
            return Context()
        top_k = int(self.cfg["retrieval"].get("top_k_per_source", 20))
        if plan.intent == Intent.EMERGENCY:
            top_k = int(self.cfg["emergency"].get("top_k", 1))
        docs = await src.search_all(
            self.searchers, plan.sources, plan.queries, text, top_k
        )
        if not docs:
            # 라우팅이 빗나가 결과가 비었을 때의 폴백
            fb = self.cfg["routing"].get("fallback_source", "guideline")
            if fb in self.searchers and fb not in plan.sources:
                docs = await self.searchers[fb].search(text, top_k)
        return rr.assemble(text, docs, self.cfg)

    async def node_l1b(self, text: str, plan: QueryPlan, session: SessionState) -> list[SafetyHit]:
        if not self.cfg["layers"].get("dur_input", True):
            return []
        names = [d.name for d in plan.entities.drugs] + session.medication_names
        if not names:
            return []
        return await dur_mod.check(names, session, text, self.dur, source="input")

    # ── L2 네이티브 보조 ────────────────────────────────────
    async def _l2_query(self, text: str, session: SessionState) -> str:
        """멀티턴 → 자기완결 질의 재작성. L2 는 single-turn 최적화다 (가이드 명시).

        1턴이면 그대로 쓴다 (재작성 호출 = 지연 + 왜곡 위험).
        멀티턴이면 L2 에게 지시어 해소를 시키되, 실패하면 원문으로 폴백한다.
        """
        if not session.history:
            return text
        hist = "\n".join(f"{h['role']}: {h['content'][:200]}" for h in session.history[-4:])
        try:
            out = await self.llm.chat("rewriter", [
                {"role": "system", "content":
                 "이전 대화를 참고해 마지막 질문을 그 자체로 완결된 한 문장으로 다시 쓰세요. "
                 "지시어(그 약, 아까 말한 것)를 모두 실제 대상으로 풀어 쓰세요. "
                 "새 정보를 추가하지 말고, 재작성된 질문 한 문장만 출력하세요."},
                {"role": "user", "content": f"[이전 대화]\n{hist}\n\n[마지막 질문]\n{text}"},
            ], max_tokens=200)
            out = (out or "").strip().strip('"')
            # 재작성이 수상하면(비었거나 너무 길거나 stub) 원문 사용
            if not out or len(out) > 300 or out.startswith("[offline"):
                return text
            return out
        except Exception:
            return text

    def _session_note(self, session: SessionState, plan: QueryPlan) -> str:
        """세션 슬롯 → 생성 단계 시스템 컨텍스트 한 덩어리."""
        parts = []
        if session.age is not None:
            parts.append(f"나이: {session.age}")
        if session.pregnant:
            parts.append("임신 중")
        rf_all = {r.type for r in session.risk_factors} | \
                 {r.type for r in plan.entities.risk_factors}
        if rf_all:
            parts.append("위험인자: " + ", ".join(sorted(rf_all)))
        if session.medication_names:
            parts.append("복용 중인 약: " + ", ".join(session.medication_names))
        if session.symptom_duration:
            parts.append(f"증상 기간: {session.symptom_duration}")
        return " / ".join(parts)

    # 응답 설계 → 생성 단계 지시문. 채점 축 하나에 지시 한 줄이 대응한다.
    _DEPTH = {
        "brief":    "짧게 답하세요. 핵심 사실 위주로 3문장 이내, 불필요한 배경 설명 금지.",
        "standard": "적당한 길이로 답하세요. 핵심 → 근거 → 주의사항 순서.",
        "thorough": "충분히 답하세요. 선택지를 비교하고 각각의 조건과 주의점을 설명하세요.",
    }

    def _l2_instructions(self, plan: QueryPlan, red_flag) -> str:
        lines = []
        if red_flag is not None:
            lines.append("‼️ 응급 가능성이 있습니다. 응급 안내(119·즉시 내원)를 "
                         "**첫 문단**에 쓰세요. 감별진단 나열을 앞세우지 마세요.")
        lines.append(self._DEPTH.get(plan.depth, self._DEPTH["standard"]))
        if plan.persona == "clinician":
            lines.append("상대는 의료인입니다. 전문 용어를 그대로 써도 됩니다.")
        else:
            lines.append("상대는 일반인입니다. 전문 용어는 풀어서 설명하세요.")
        if plan.need_followup and plan.clarifying_question:
            # ★ 되묻기는 '질문만 던지기'가 아니다. 3턴뿐이라 한 턴을 질문에만 쓰면 손해다.
            lines.append(
                f"답변을 먼저 한 뒤, 마지막에 이 질문 **하나만** 덧붙이세요: "
                f"「{plan.clarifying_question}」 다른 질문을 추가로 만들지 마세요.")
        else:
            lines.append("되묻지 말고 지금 정보로 답을 완성하세요.")
        if plan.uncertainty:
            lines.append(f"다음은 불확실하다고 명시하세요: {plan.uncertainty}")
        else:
            lines.append("확실한 사실에 불필요한 유보 표현을 붙이지 마세요.")
        if plan.expects_drug_output:
            lines.append("약을 언급한다면 금기·주의(임신·음주·병용·연령·간신장)를 "
                         "반드시 함께 안내하세요.")
        return "\n".join(f"- {x}" for x in lines)

    async def node_l4(self, text, plan, ctx, session, input_hits, red_flag) -> str:
        """L4 · 초안 생성 + 🚨 응급 응답 빌더."""
        if self.l2 is not None:
            # L2 2단계 경로 — 검색은 L2 가 retrieve_relevant_content 로 스스로 한다
            query = await self._l2_query(text, session)
            answer = await self.l2.generate(
                query,
                intent=plan.intent,
                context_note=self._session_note(session, plan),
                instructions=self._l2_instructions(plan, red_flag),
            )
        else:
            answer = await gen.draft(text, plan, ctx, session, input_hits, self.llm, self.cfg)

        # 응급 안내를 문자열 조립 단계에서 첫 문단으로 확정한다.
        # 모델이 뭘 쓰든 그 앞에 붙는다. 뒤에 묻으면 -9 이므로 재량에 맡기지 않는다.
        if red_flag is not None:
            prefix = gen.emergency_prefix(red_flag.category)
            if prefix.split(".")[0][:12] not in answer[:250]:
                answer = prefix + "\n\n" + answer
        return answer

    async def node_l4b(self, text, plan, session, answer, input_hits):
        """L4b · 출력측 안전 게이트. 모델이 '추천한' 약을 검증한다."""
        out_hits, mentioned = await gen.output_gate(
            answer, plan, session, self.dur, self.llm, self.cfg
        )
        all_hits = dur_mod.dedupe(input_hits + out_hits)
        critical, notable = dur_mod.split_by_severity(all_hits)

        if critical:
            # 절대 금기는 경고 삽입이 아니라 재작성.
            # 삽입만 하면 "타이레놀 드세요 / 타이레놀 드시면 안 됩니다" 자기모순이 된다.
            for _ in range(2):
                answer = await gen.rewrite(text, answer, critical, session, self.llm)
                out_hits2, _ = await gen.output_gate(
                    answer, plan, session, self.dur, self.llm, self.cfg
                )
                critical2, _ = dur_mod.split_by_severity(dur_mod.dedupe(out_hits2))
                if not critical2:
                    break
                critical = critical2
            else:
                answer = gen.strip_drug_recommendation(answer)

        if notable:
            warn = dur_mod.render_warning(notable)
            if warn.split("\n")[0] not in answer:
                answer = warn + "\n\n" + answer

        return answer, all_hits, mentioned

    async def node_l4c(self, text, answer, plan, ctx, session, hits, deadline=None):
        """L4c · 루브릭 비평 패스."""
        return await critique_loop(
            text, answer, plan, ctx, session, hits, self.llm, self.cfg, deadline
        )

    async def run_passthrough(self, text: str, session: SessionState) -> TurnResult:
        """파이프라인 없이 모델에 그대로 묻는다 — 넘어야 할 베이스라인.

        L1-16B-A3B 는 HealthBench-Consensus 93.5% 를 이미 낸다.
        우리 파이프라인이 그 숫자를 못 넘으면 노이즈를 넣고 있는 것이다.
        반드시 같은 조건에서 비교할 것: make ab-baseline
        """
        t0 = time.perf_counter()
        if self.l2 is not None:
            # L2 네이티브 베이스라인 — 권장 2단계 사용법 그대로, 우리 레이어 없이.
            query = await self._l2_query(text, session)
            answer = await self.l2.generate(query)
        else:
            msgs = [{"role": "system",
                     "content": "당신은 대국민 건강 상담 챗봇입니다. 정확하고 안전하게 답하세요."}]
            msgs += [{"role": h["role"], "content": h["content"]} for h in session.history[-6:]]
            msgs.append({"role": "user", "content": text})
            answer = await self.llm.chat("drafter", msgs)
        sess.update(session, text, QueryPlan(), answer)
        return TurnResult(
            answer=answer, plan=QueryPlan(),
            latency_ms={"total": int((time.perf_counter() - t0) * 1000)},
            trace={"passthrough": True},
        )

    # ── 한 턴 전체 ──────────────────────────────────────────
    async def run_turn(self, text: str, session: SessionState) -> TurnResult:
        if self.cfg["layers"].get("passthrough", False):
            return await self.run_passthrough(text, session)

        t_start = time.perf_counter()
        lat: dict[str, int] = {}
        deadline = t_start + float(self.cfg["generation"].get("total_budget_sec", 20.0))

        def mark(k: str, t0: float) -> None:
            lat[k] = int((time.perf_counter() - t0) * 1000)

        # L1
        t = time.perf_counter()
        red_flag = self.node_l1(text, session)
        mark("l1", t)

        # L2
        t = time.perf_counter()
        plan = await self.node_l2(text, session, red_flag)
        mark("l2", t)

        # L3 + L1b — 서로 의존하지 않으므로 병렬. 0.5초가 공짜로 사라진다.
        t = time.perf_counter()
        ctx, input_hits = await asyncio.gather(
            self.node_l3(text, plan),
            self.node_l1b(text, plan, session),
        )
        mark("l3_l1b", t)

        # L4
        t = time.perf_counter()
        answer = await self.node_l4(text, plan, ctx, session, input_hits, red_flag)
        mark("l4", t)

        # L4b — 모델이 '추천한' 약을 검증
        t = time.perf_counter()
        answer, all_hits, mentioned = await self.node_l4b(
            text, plan, session, answer, input_hits
        )
        mark("l4b", t)

        # L4c
        t = time.perf_counter()
        answer, crit = await self.node_l4c(
            text, answer, plan, ctx, session, all_hits, deadline
        )
        mark("l4c", t)

        lat["total"] = int((time.perf_counter() - t_start) * 1000)

        sess.update(session, text, plan, answer)

        return TurnResult(
            answer=answer,
            plan=plan,
            context=ctx,
            safety_hits=all_hits,
            critique=crit,
            latency_ms=lat,
            trace={
                "sources": plan.sources,
                "red_flag": red_flag.category if red_flag else None,
                "ctx_docs": len(ctx.docs),
                "ctx_tokens": ctx.tokens_used,
                "ctx_dropped": ctx.dropped,
                "drugs_in": [d.name for d in plan.entities.drugs],
                "drugs_out": mentioned,
                "l2": (dict(self.l2.trace) if self.l2 is not None else None),
                "violations": [v.item for v in (crit.violations if crit else [])],
            },
        )

    async def run_conversation(
        self, turns: list[str], session: SessionState | None = None
    ) -> list[TurnResult]:
        session = session or sess.new()
        out = []
        for t in turns:
            out.append(await self.run_turn(t, session))
        return out
