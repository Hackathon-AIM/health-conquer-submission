import asyncio

import pytest

from medibot.clinical.analyzer import QueryAnalyzer
from medibot.config.settings import Settings
from medibot.core.schemas import ClinicalClaim
from medibot.core.schemas import ClinicalState
from medibot.core.schemas import QueryAnalysis
from medibot.core.schemas import ResponseRequirements
from medibot.core.schemas import RetrievalEvidence
from medibot.reasoning.generator import MedicalGenerator
from medibot.response.composer import ResponseComposer
from medibot.retrieval.coverage import CoverageCheck
from medibot.retrieval.evidence_filter import EvidenceFilter
from medibot.retrieval.query_planner import SourceQueryPlanner
from medibot.retrieval.reranker import Reranker
from medibot.retrieval.router import DynamicSourceRouter
from medibot.sources.registry import SourceRegistry
from medibot.sources.mini import MiniEvidenceSource
from medibot.verification.claim_extractor import ClaimExtractor
from medibot.verification.groundedness import GroundednessVerifier
from medibot.verification.safety import ClinicalSafetyVerifier


def test_hypertension_ibuprofen_routes_drug_and_guideline() -> None:
    analysis = QueryAnalyzer().analyze("고혈압약 먹는데 이부프로펜 먹어도 돼?", "NON_EMERGENT")
    queries = SourceQueryPlanner().plan("고혈압약 먹는데 이부프로펜 먹어도 돼?", analysis)
    quotas = DynamicSourceRouter().route(analysis, queries)
    assert "drug" in quotas
    assert "guideline" in quotas


def test_evidence_limit_and_coverage_for_medication_question() -> None:
    async def run() -> None:
        analysis = QueryAnalyzer().analyze("고혈압약 먹는데 이부프로펜 먹어도 돼?", "NON_EMERGENT")
        queries = SourceQueryPlanner().plan("고혈압약 먹는데 이부프로펜 먹어도 돼?", analysis)
        quotas = DynamicSourceRouter().route(analysis, queries)
        source = MiniEvidenceSource()
        evidence = []
        for source_name, top_k in quotas.items():
            for query in queries[source_name]:
                evidence.extend(await source.search(query, top_k))
        ranked = Reranker().rank(evidence)
        final = EvidenceFilter().filter(ranked, limit=3)
        coverage = CoverageCheck().check(analysis, final)
        assert len(final) <= 3
        assert coverage["sufficient"] is True

    asyncio.run(run())


def test_source_registry_mini_backend_routes_logical_sources() -> None:
    async def run() -> None:
        registry = SourceRegistry(Settings(rag_backend="mini"))
        evidence, diagnostic = await registry.search(
            "drug",
            "ibuprofen hypertension interaction safety",
            top_k=3,
            filters={"source": "drug"},
        )
        assert evidence
        assert diagnostic["adapter"] == "mini"
        assert diagnostic["status"] == "success"

    asyncio.run(run())


def test_lunit_mcp_backend_traces_unconfigured_stub() -> None:
    async def run() -> None:
        registry = SourceRegistry(
            Settings(rag_backend="lunit_mcp", enabled_mcp_sources=["drug_label"])
        )
        evidence, diagnostic = await registry.search(
            "drug",
            "ibuprofen hypertension interaction safety",
            top_k=3,
            filters={"source": "drug"},
        )
        assert evidence == []
        assert diagnostic["adapter"] == "drug_label"
        assert diagnostic["status"] == "stub_unconfigured"

    asyncio.run(run())


def test_l2_final_guard_blocks_non_l2_provider() -> None:
    async def run() -> None:
        generator = MedicalGenerator(
            Settings(
                model_api_base=None,
                final_model_provider="fallback",
                require_l2_final=True,
            )
        )
        with pytest.raises(RuntimeError, match="Lunit L2"):
            await generator.generate(
                "hello",
                ClinicalState(session_id="test"),
                QueryAnalysis(intent="general_health_information"),
                ResponseRequirements(),
                [],
            )

    asyncio.run(run())


def test_high_risk_claim_without_evidence_is_unsupported() -> None:
    result = GroundednessVerifier().verify(
        [
            ClinicalClaim(
                id="c1",
                text="이 약은 반드시 안전합니다.",
                claim_type="interaction",
                evidence_ids=[],
            )
        ],
        [RetrievalEvidence(id="e1", source="drug", content="context")],
    )
    assert result["passed"] is False
    assert result["unsupported_claim_ids"] == ["c1"]
    assert result["blocking_claim_ids"] == ["c1"]


def test_advisory_unsupported_claim_does_not_trigger_fallback() -> None:
    draft = "This may increase the risk of symptoms getting worse."
    groundedness = GroundednessVerifier().verify(
        [
            ClinicalClaim(
                id="c1",
                text=draft,
                claim_type="risk",
                evidence_ids=[],
            )
        ],
        [],
    )
    safety = ClinicalSafetyVerifier().verify(draft, "NON_EMERGENT", {}, groundedness)
    answer, status = ResponseComposer().compose(
        draft,
        "NON_EMERGENT",
        QueryAnalysis(intent="general_health_information"),
        [],
        safety,
    )
    assert groundedness["passed"] is True
    assert safety["passed"] is True
    assert safety["advisory_issues"] == ["unsupported_advisory_claim"]
    assert status == "pass"
    assert draft in answer


def test_general_hypertension_screening_sentence_is_not_interaction() -> None:
    draft = "만약 가족력이 있거나 고혈압·당뇨병이 있다면 정기 검진을 통해 수치를 확인해 보시는 것이 좋아요."
    claims = ClaimExtractor().extract(draft, [])
    groundedness = GroundednessVerifier().verify(claims, [])
    safety = ClinicalSafetyVerifier().verify(draft, "NON_EMERGENT", {}, groundedness)
    answer, status = ResponseComposer().compose(
        draft,
        "NON_EMERGENT",
        QueryAnalysis(intent="general_health_information"),
        [],
        safety,
    )
    assert [claim.claim_type for claim in claims] == ["other"]
    assert groundedness["passed"] is True
    assert safety["passed"] is True
    assert status == "pass"
    assert draft in answer


def test_blood_pressure_medication_interaction_stays_blocking_without_evidence() -> None:
    draft = "이부프로펜 같은 NSAID는 혈압을 올리거나 일부 혈압약의 효과를 약하게 만들 수 있습니다."
    claims = ClaimExtractor().extract(draft, [])
    groundedness = GroundednessVerifier().verify(claims, [])
    safety = ClinicalSafetyVerifier().verify(draft, "NON_EMERGENT", {}, groundedness)
    assert [claim.claim_type for claim in claims] == ["interaction"]
    assert groundedness["passed"] is False
    assert safety["passed"] is False
    assert safety["blocking_issues"] == ["unsupported_blocking_claim"]


def test_lab_unit_mg_dl_is_not_dosage_claim() -> None:
    draft = "정상 수치는 보통 LDL 100 mg/dL 미만을 권장합니다."
    claims = ClaimExtractor().extract(draft, [])
    groundedness = GroundednessVerifier().verify(claims, [])
    safety = ClinicalSafetyVerifier().verify(draft, "NON_EMERGENT", {}, groundedness)
    assert [claim.claim_type for claim in claims] == ["other"]
    assert groundedness["passed"] is True
    assert safety["passed"] is True


def test_unsupported_dosage_claim_is_blocking() -> None:
    draft = "Take 800 mg every day."
    groundedness = GroundednessVerifier().verify(
        [
            ClinicalClaim(
                id="c1",
                text=draft,
                claim_type="dosage",
                evidence_ids=[],
            )
        ],
        [],
    )
    safety = ClinicalSafetyVerifier().verify(draft, "NON_EMERGENT", {}, groundedness)
    assert groundedness["passed"] is False
    assert safety["passed"] is False
    assert safety["blocking_issues"] == ["unsupported_blocking_claim"]


def test_emergency_answer_without_action_is_blocking() -> None:
    safety = ClinicalSafetyVerifier().verify(
        "You may be having serious symptoms.",
        "EMERGENT",
        {},
        {"passed": True, "blocking_claim_ids": [], "advisory_claim_ids": []},
    )
    assert safety["passed"] is False
    assert safety["blocking_issues"] == ["missing_emergency_action"]
