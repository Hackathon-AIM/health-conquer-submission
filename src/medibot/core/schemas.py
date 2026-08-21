from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]
TriageClass = Literal["EMERGENT", "CONDITIONALLY_EMERGENT", "NON_EMERGENT"]
RetrievalNeed = Literal["required", "optional", "skip"]
SafetyStatus = Literal["pass", "fallback", "blocked"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class MedibotRequest(BaseModel):
    session_id: str | None = None
    messages: list[ChatMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


class MedibotResponse(BaseModel):
    trace_id: str
    answer: str
    triage_class: TriageClass
    retrieval_used: bool
    evidence_ids: list[str] = Field(default_factory=list)
    safety_status: SafetyStatus


class ClinicalState(BaseModel):
    session_id: str
    jurisdiction: str | None = None
    locale: str | None = None
    user_asserted_facts: dict[str, Any] = Field(default_factory=dict)
    verified_medical_facts: dict[str, Any] = Field(default_factory=dict)
    assistant_previous_claims: dict[str, Any] = Field(default_factory=dict)
    uncertain_inferences: dict[str, Any] = Field(default_factory=dict)
    turn_index: int = 0
    turns_remaining: int = 3


class QueryAnalysis(BaseModel):
    intent: str
    complexity: Literal["simple", "medium", "complex"] = "simple"
    risk_class: Literal["low", "medium", "high", "emergency"] = "low"
    jurisdiction: str | None = None
    jurisdiction_confidence: float = 0.0
    uncertainty_type: Literal["none", "reducible", "irreducible"] = "none"
    missing_information: list[str] = Field(default_factory=list)
    retrieval_need: RetrievalNeed = "skip"
    graph_reasoning_candidate: bool = False
    diseases: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    drugs: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)


class ConversationDecision(BaseModel):
    expected_information_gain: Literal["low", "medium", "high"] = "low"
    can_answer_safely_now: bool = True
    decision: Literal["ask", "answer"] = "answer"
    follow_up_question: str | None = None


class ResponseRequirements(BaseModel):
    must_include: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    audience: Literal["layperson", "caregiver", "professional"] = "layperson"
    depth: Literal["short", "medium", "detailed"] = "medium"
    direct_answer_first: bool = True


class RetrievalEvidence(BaseModel):
    id: str
    source: str
    title: str | None = None
    content: str
    source_id: str | None = None
    cite_uid: str | None = None
    source_url: str | None = None
    corpus_tag: str | None = None
    page_range: str | None = None
    raw_tool_name: str | None = None
    raw_result: Any | None = None
    authority_score: float = 0.0
    relevance_score: float = 0.0
    utility_score: float = 0.0
    published_at: str | None = None
    updated_at: str | None = None
    jurisdiction: str | None = None
    supports: list[str] = Field(default_factory=list)

    @property
    def total_score(self) -> float:
        return self.authority_score + self.relevance_score + self.utility_score


class ClinicalClaim(BaseModel):
    id: str
    text: str
    claim_type: Literal[
        "medical_fact",
        "diagnosis",
        "dosage",
        "interaction",
        "recommendation",
        "risk",
        "other",
    ] = "other"
    evidence_ids: list[str] = Field(default_factory=list)


class TraceRecord(BaseModel):
    trace_id: str
    session_id: str
    user_message: str
    assistant_response: str | None = None
    jurisdiction: str | None = None
    triage_class: TriageClass
    query_analysis: dict[str, Any] = Field(default_factory=dict)
    source_queries: dict[str, list[str]] = Field(default_factory=dict)
    selected_sources: list[str] = Field(default_factory=list)
    retrieval_results: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    final_evidence: list[str] = Field(default_factory=list)
    evidence_coverage_result: dict[str, Any] = Field(default_factory=dict)
    graph_enabled: bool = False
    graph_nodes: int = 0
    graph_edges: int = 0
    claims: list[dict[str, Any]] = Field(default_factory=list)
    groundedness_result: dict[str, Any] = Field(default_factory=dict)
    safety_result: dict[str, Any] = Field(default_factory=dict)
    fallback_reason: str | None = None
    latency_ms: dict[str, float] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    feature_flags: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    generation_mode: str | None = None
    generation_error: str | None = None
    final_model_provider: str | None = None
    final_model_name: str | None = None
    l2_required: bool = False
    l2_retrieval_phase_status: str | None = None
    l2_mcp_tool_call_count: int = 0
    l2_selected_cite_uids: list[str] = Field(default_factory=list)
    l2_finalize_note: str | None = None
