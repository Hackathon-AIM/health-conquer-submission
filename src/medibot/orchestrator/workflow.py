import asyncio
import time
from uuid import uuid4

from medibot.clinical.analyzer import QueryAnalyzer
from medibot.clinical.conversation_policy import ConversationPolicyManager
from medibot.clinical.emergency import EmergencyFastPath
from medibot.clinical.state_manager import ClinicalStateManager
from medibot.clinical.triage import SafetyPreTriage
from medibot.config.feature_flags import FeatureFlags
from medibot.config.settings import Settings
from medibot.core.prompts import PromptLoader
from medibot.core.schemas import (
    ClinicalClaim,
    MedibotRequest,
    MedibotResponse,
    QueryAnalysis,
    RetrievalEvidence,
    TraceRecord,
)
from medibot.observability.trace import TraceWriter
from medibot.policy.response_requirements import ResponseRequirementPlanner
from medibot.policy.retrieval_policy import RetrievalPolicy
from medibot.reasoning.generator import MedicalGenerator
from medibot.response.composer import ResponseComposer
from medibot.retrieval.coverage import CoverageCheck
from medibot.retrieval.evidence_filter import EvidenceFilter
from medibot.retrieval.fusion import EvidenceFusion
from medibot.retrieval.query_planner import SourceQueryPlanner
from medibot.retrieval.reranker import Reranker
from medibot.retrieval.router import DynamicSourceRouter
from medibot.sources.registry import SourceRegistry
from medibot.verification.claim_extractor import ClaimExtractor
from medibot.verification.groundedness import GroundednessVerifier
from medibot.verification.safety import ClinicalSafetyVerifier


class MedibotWorkflow:
    """P0 deterministic workflow."""

    def __init__(
        self,
        settings: Settings | None = None,
        flags: FeatureFlags | None = None,
        state_manager: ClinicalStateManager | None = None,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.flags = flags or FeatureFlags()
        self.state_manager = state_manager or ClinicalStateManager()
        self.trace_writer = trace_writer or TraceWriter(self.settings.trace_path)
        self.prompt_loader = PromptLoader(self.settings.prompt_dir)
        self.triage = SafetyPreTriage()
        self.emergency = EmergencyFastPath()
        self.analyzer = QueryAnalyzer()
        self.conversation_policy = ConversationPolicyManager()
        self.requirement_planner = ResponseRequirementPlanner()
        self.retrieval_policy = RetrievalPolicy()
        self.query_planner = SourceQueryPlanner()
        self.router = DynamicSourceRouter()
        self.source_registry = SourceRegistry(self.settings)
        self.reranker = Reranker()
        self.evidence_filter = EvidenceFilter()
        self.coverage = CoverageCheck()
        self.fusion = EvidenceFusion()
        self.generator = MedicalGenerator(self.settings)
        self.claim_extractor = ClaimExtractor()
        self.groundedness = GroundednessVerifier()
        self.safety = ClinicalSafetyVerifier()
        self.composer = ResponseComposer()

    async def handle(self, request: MedibotRequest) -> MedibotResponse:
        start = time.perf_counter()
        if not request.messages:
            raise ValueError("messages must contain at least one item")
        latest_user = next(
            (msg for msg in reversed(request.messages) if msg.role == "user"),
            None,
        )
        if latest_user is None:
            raise ValueError("messages must contain a user message")

        state = self.state_manager.get_or_create(request.session_id)
        state = self.state_manager.update_from_messages(state, request.messages)
        trace_id = str(uuid4())
        triage_class = self.triage.classify(latest_user.content)
        analysis = self.analyzer.analyze(latest_user.content, triage_class)
        if analysis.jurisdiction:
            state.jurisdiction = analysis.jurisdiction
        decision = self.conversation_policy.decide(analysis, state)
        requirements = self.requirement_planner.plan(analysis, triage_class)

        source_queries: dict[str, list[str]] = {}
        selected_sources: list[str] = []
        raw_evidence: list[RetrievalEvidence] = []
        final_evidence: list[RetrievalEvidence] = []
        retrieval_diagnostics: list[dict[str, object]] = []
        coverage_result: dict[str, object] = {"sufficient": True}
        fallback_reason: str | None = None

        claims: list[ClinicalClaim] = []
        groundedness_result: dict[str, object] = {}
        safety_result: dict[str, object] = {}
        answer: str | None = None
        safety_status = "blocked"
        draft = ""

        try:
            if triage_class == "EMERGENT":
                draft = self.emergency.compose(latest_user.content)
                claims = self.claim_extractor.extract(draft, [])
                groundedness_result = {"passed": True, "unsupported_claim_ids": []}
                safety_result = self.safety.verify(
                    draft, triage_class, coverage_result, groundedness_result
                )
                answer, safety_status = draft, "pass"
            else:
                if decision.decision == "ask" and decision.follow_up_question:
                    draft = decision.follow_up_question
                else:
                    if (
                        self.retrieval_policy.should_retrieve(analysis)
                        and self.settings.final_model_provider != "lunit_l2"
                    ):
                        source_queries = self.query_planner.plan(latest_user.content, analysis)
                        quotas = self.router.route(analysis, source_queries)
                        selected_sources = list(quotas)
                        raw_evidence, retrieval_diagnostics = await self._retrieve(
                            source_queries, quotas
                        )
                        ranked = self.reranker.rank(raw_evidence)
                        fused = self.fusion.fuse(ranked)
                        final_evidence = self.evidence_filter.filter(
                            fused, self.settings.evidence_limit
                        )
                        if self.flags.enable_evidence_coverage_check:
                            coverage_result = self.coverage.check(analysis, final_evidence)
                    draft = await self.generator.generate(
                        latest_user.content, state, analysis, requirements, final_evidence
                    )
                    if self.generator.last_evidence:
                        final_evidence = self.generator.last_evidence[
                            : self.settings.evidence_limit
                        ]
                        raw_evidence.extend(final_evidence)
                        if self.flags.enable_evidence_coverage_check:
                            coverage_result = self.coverage.check(analysis, final_evidence)

                claims = self.claim_extractor.extract(draft, final_evidence)
                groundedness_result = self.groundedness.verify(claims, final_evidence)
                safety_result = self.safety.verify(
                    draft, triage_class, coverage_result, groundedness_result
                )
                blocking_issues = safety_result.get("blocking_issues", [])
                if blocking_issues:
                    fallback_reason = ",".join(str(issue) for issue in blocking_issues)
                answer, safety_status = self.composer.compose(
                    draft, triage_class, analysis, final_evidence, safety_result
                )
        except Exception as exc:
            if self.generator.last_error is None:
                self.generator.last_error = type(exc).__name__
            safety_result = {
                "passed": False,
                "issues": ["pipeline_exception"],
                "blocking_issues": ["pipeline_exception"],
                "exception_type": type(exc).__name__,
            }
            fallback_reason = type(exc).__name__
            trace = self._build_trace(
                trace_id=trace_id,
                state_session_id=state.session_id,
                user_message=latest_user.content,
                assistant_response=None,
                jurisdiction=state.jurisdiction,
                triage_class=triage_class,
                analysis=analysis,
                source_queries=source_queries,
                selected_sources=selected_sources,
                raw_evidence=raw_evidence,
                final_evidence=final_evidence,
                retrieval_diagnostics=retrieval_diagnostics,
                coverage_result=coverage_result,
                claims=claims,
                groundedness_result=groundedness_result,
                safety_result=safety_result,
                fallback_reason=fallback_reason,
                started=start,
            )
            self.trace_writer.write(trace)
            raise

        trace = self._build_trace(
            trace_id=trace_id,
            state_session_id=state.session_id,
            user_message=latest_user.content,
            assistant_response=answer,
            jurisdiction=state.jurisdiction,
            triage_class=triage_class,
            source_queries=source_queries,
            selected_sources=selected_sources,
            raw_evidence=raw_evidence,
            final_evidence=final_evidence,
            retrieval_diagnostics=retrieval_diagnostics,
            coverage_result=coverage_result,
            analysis=analysis,
            claims=claims,
            groundedness_result=groundedness_result,
            safety_result=safety_result,
            fallback_reason=fallback_reason,
            started=start,
        )
        self.trace_writer.write(trace)
        return MedibotResponse(
            trace_id=trace_id,
            answer=answer,
            triage_class=triage_class,
            retrieval_used=bool(final_evidence),
            evidence_ids=[item.id for item in final_evidence],
            safety_status=safety_status,
        )

    def _build_trace(
        self,
        trace_id: str,
        state_session_id: str,
        user_message: str,
        assistant_response: str | None,
        jurisdiction: str | None,
        triage_class: str,
        analysis: QueryAnalysis,
        source_queries: dict[str, list[str]],
        selected_sources: list[str],
        raw_evidence: list[RetrievalEvidence],
        final_evidence: list[RetrievalEvidence],
        retrieval_diagnostics: list[dict[str, object]],
        coverage_result: dict[str, object],
        claims: list[ClinicalClaim],
        groundedness_result: dict[str, object],
        safety_result: dict[str, object],
        fallback_reason: str | None,
        started: float,
    ) -> TraceRecord:
        return TraceRecord(
            trace_id=trace_id,
            session_id=state_session_id,
            user_message=user_message,
            assistant_response=assistant_response,
            jurisdiction=jurisdiction,
            triage_class=triage_class,
            query_analysis=analysis.model_dump(mode="json"),
            source_queries=source_queries,
            selected_sources=selected_sources,
            retrieval_results=[
                self._trace_evidence(item) for item in raw_evidence
            ],
            retrieval_diagnostics=retrieval_diagnostics,
            final_evidence=[item.id for item in final_evidence],
            evidence_coverage_result=coverage_result,
            graph_enabled=self.flags.enable_graph_reasoning,
            claims=[claim.model_dump(mode="json") for claim in claims],
            groundedness_result=groundedness_result,
            safety_result=safety_result,
            fallback_reason=fallback_reason,
            latency_ms={"total": (time.perf_counter() - started) * 1000},
            feature_flags=self.flags.model_dump(mode="json"),
            prompt_versions=self.prompt_loader.versions_for(
                [
                    "system.md",
                    "query_analyzer.md",
                    "retrieval_query_planner.md",
                    "answer_generator.md",
                    "claim_extractor.md",
                    "groundedness_verifier.md",
                    "safety_verifier.md",
                ]
            ),
            generation_mode=self.generator.last_mode,
            generation_error=self.generator.last_error,
            final_model_provider=self.settings.final_model_provider,
            final_model_name=self.settings.model_name,
            l2_required=self.settings.require_l2_final,
            l2_retrieval_phase_status=self.generator.last_l2_trace.get(
                "l2_retrieval_phase_status"
            ),
            l2_mcp_tool_call_count=int(
                self.generator.last_l2_trace.get("l2_mcp_tool_call_count") or 0
            ),
            l2_selected_cite_uids=list(
                self.generator.last_l2_trace.get("l2_selected_cite_uids") or []
            ),
            l2_finalize_note=self.generator.last_l2_trace.get("l2_finalize_note"),
        )

    def _trace_evidence(self, evidence: RetrievalEvidence) -> dict[str, object]:
        limit = self.settings.trace_content_char_limit
        content = evidence.content
        if limit > 0 and len(content) > limit:
            content = content[:limit] + "\n[truncated]"
        return {
            "id": evidence.id,
            "source": evidence.source,
            "title": evidence.title,
            "content_preview": content,
            "source_id": evidence.source_id,
            "cite_uid": evidence.cite_uid,
            "source_url": evidence.source_url,
            "corpus_tag": evidence.corpus_tag,
            "page_range": evidence.page_range,
            "raw_tool_name": evidence.raw_tool_name,
            "authority_score": evidence.authority_score,
            "relevance_score": evidence.relevance_score,
            "utility_score": evidence.utility_score,
            "published_at": evidence.published_at,
            "updated_at": evidence.updated_at,
            "jurisdiction": evidence.jurisdiction,
            "supports": evidence.supports,
        }

    async def _retrieve(
        self, source_queries: dict[str, list[str]], quotas: dict[str, int]
    ) -> tuple[list[RetrievalEvidence], list[dict[str, object]]]:
        tasks = []
        for source, top_k in quotas.items():
            for query in source_queries.get(source, []):
                tasks.append(
                    self.source_registry.search(
                        source, query, top_k=top_k, filters={"source": source}
                    )
                )
        if not tasks:
            return [], []
        groups = await asyncio.gather(*tasks)
        evidence: list[RetrievalEvidence] = []
        diagnostics: list[dict[str, object]] = []
        for group_evidence, diagnostic in groups:
            evidence.extend(group_evidence)
            diagnostics.append(diagnostic)
        return evidence, diagnostics
