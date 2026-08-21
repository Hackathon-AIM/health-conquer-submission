from medibot.core.schemas import RetrievalEvidence


class MiniEvidenceSource:
    """Small deterministic source corpus for P0 local smoke tests."""

    name = "mini"

    def __init__(self) -> None:
        self._items = [
            RetrievalEvidence(
                id="mini-emergency-red-flags",
                source="guideline",
                title="Emergency red flags",
                content=(
                    "Severe chest pain, severe shortness of breath, stroke-like symptoms, "
                    "altered consciousness, anaphylaxis, and severe bleeding require urgent emergency care."
                ),
                authority_score=0.9,
                utility_score=0.9,
                supports=["emergency_referral", "red_flags"],
            ),
            RetrievalEvidence(
                id="mini-nsaid-bp",
                source="drug",
                title="NSAIDs and blood pressure",
                content=(
                    "NSAIDs such as ibuprofen may raise blood pressure and can reduce the effect of some "
                    "antihypertensive medicines. People with hypertension should ask a clinician or pharmacist "
                    "before repeated use."
                ),
                authority_score=0.85,
                utility_score=0.9,
                supports=["interaction", "blood_pressure_effect", "medication_safety"],
            ),
            RetrievalEvidence(
                id="mini-nsaid-kidney-risk",
                source="drug",
                title="NSAID kidney risk",
                content=(
                    "NSAIDs may increase kidney risk, especially in older adults, dehydration, chronic kidney "
                    "disease, or when combined with some blood pressure medicines."
                ),
                authority_score=0.8,
                utility_score=0.8,
                supports=["risk", "warning_signs"],
            ),
            RetrievalEvidence(
                id="mini-guideline-self-care",
                source="guideline",
                title="General self-care guidance",
                content=(
                    "For non-emergency symptoms, give practical self-care advice, explain uncertainty, and list "
                    "red flags that should prompt medical care."
                ),
                authority_score=0.7,
                utility_score=0.7,
                supports=["communication", "red_flags"],
            ),
            RetrievalEvidence(
                id="mini-pharmacist-next-step",
                source="guideline",
                title="Medication safety next step",
                content=(
                    "When a medication interaction question depends on the patient's medicines, conditions, or "
                    "kidney function, recommend checking with a clinician or pharmacist before use."
                ),
                authority_score=0.8,
                utility_score=0.85,
                supports=["safe_next_action", "interaction"],
            ),
        ]

    async def search(
        self, query: str, top_k: int, filters: dict | None = None
    ) -> list[RetrievalEvidence]:
        terms = {term for term in query.lower().replace("/", " ").split() if len(term) > 2}
        scored: list[RetrievalEvidence] = []
        for item in self._items:
            haystack = " ".join([item.source, item.title or "", item.content, *item.supports]).lower()
            overlap = sum(1 for term in terms if term in haystack)
            if overlap or not terms:
                clone = item.model_copy()
                clone.relevance_score = min(1.0, 0.2 + overlap * 0.2)
                scored.append(clone)
        return sorted(scored, key=lambda item: item.total_score, reverse=True)[:top_k]
